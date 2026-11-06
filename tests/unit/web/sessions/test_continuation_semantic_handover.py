"""Drain classification requires a real quiescent retained semantic source."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from hashlib import sha256
from uuid import UUID, uuid4

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_next_action as next_action_module
from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EndFrame, ToolBatchFrame
from elspeth.web.composer.continuation_clock import SegmentFit
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_handoff import classify_drain_cut, classify_drain_cut_on_connection
from elspeth.web.composer.continuation_progress import decode_continuation_frontier
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.composer_finalization import _message_with_state_response
from elspeth.web.sessions.composer_operation_errors import worker_lost_error
from elspeth.web.sessions.composer_operations import ComposerOperationAssistantWrite, ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.continuation_semantic_actions import require_semantic_publication_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as retained_effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.routes._helpers import composer_turn_end_assistant_row
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    observe_original_driver_failures,
    semantic_primary_response,
    semantic_read_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_end_fixture
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture
from tests.unit.web.sessions.test_continuation_semantic_owned_publication import _ordinary_recovery_stop, actual_owned_recovery_case
from tests.unit.web.sessions.test_continuation_semantic_publication import actual_semantic_publication_case

operation_store = semantic_fixture.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("clean", "blocked"))
async def test_completed_review_cold_handover_reproves_original_result(operation_store, tmp_path, monkeypatch, outcome):
    async with shared_end_fixture.actual_shared_end_case(operation_store, tmp_path, monkeypatch, outcome=outcome) as case:
        engine, repository, authority, _sessions, sid = operation_store
        original = case["execution"].review
        running, work, lease = case["running"], case["work"], case["lease"]
        stored = original.completed
        names = case["runtime"].env_ref_names
        original_calls = tuple(case["sdk"])
        with engine.connect() as conn:
            request_id = conn.execute(
                select(semantic_fixture.jobs.c.request_id).where(
                    semantic_fixture.jobs.c.session_id == str(sid), semantic_fixture.jobs.c.operation_id == running.claim.operation_id
                )
            ).scalar_one()
        original_require = next_action_module.require_completed_end_review_source_on_connection
        boundary, corrupt_at, observed = "idle", None, []

        def rows():
            with engine.connect() as conn:
                return semantic_fixture._rows_on_connection(conn), tuple(
                    tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all()) for table in (attempts, segments, fences)
                )

        def require_original(conn, *, session_id, operation_id, selected):
            nonlocal corrupt_at
            if session_id == str(sid) and operation_id == running.claim.operation_id:
                observed.append(boundary)
                if corrupt_at == boundary:
                    corrupt_at = None
                    key = conn.execute(
                        select(retained_effects.c.result_chunk_key).where(
                            retained_effects.c.session_id == session_id,
                            retained_effects.c.operation_id == operation_id,
                            retained_effects.c.effect_key == original.owner_effect_key,
                        )
                    ).scalar_one()
                    body = json.loads(read_chunk(conn, session_id=session_id, operation_id=operation_id, key=key))
                    body["outcome"] = "blocked" if outcome == "clean" else "clean"
                    encoded = canonical_json(body).encode()
                    changed = conn.execute(
                        update(chunks)
                        .where(chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key)
                        .values(payload=encoded, byte_length=len(encoded), sha256=sha256(encoded).hexdigest())
                    )
                    assert changed.rowcount == 1
            return original_require(conn, session_id=session_id, operation_id=operation_id, selected=selected)

        monkeypatch.setattr(next_action_module, "require_completed_end_review_source_on_connection", require_original)
        try:
            for recovery_index in range(2):
                selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
                assert type(selected.stack.frames[-1]) is EndFrame
                assert selected.stack.frames[-1].phase == ("publication_ready" if outcome == "clean" else "repair_needed")
                boundary = "poll"
                prior = len(observed)
                assert authority.read_continuation_tool_failure(running, env_ref_names=names) is None
                assert len(observed) == prior
                boundary = "classify"
                with locked_session_transaction(engine, str(sid)) as conn:
                    assert classify_drain_cut_on_connection(conn, running, selected, env_ref_names=names) == "handover"
                    before = semantic_fixture._rows_on_connection(conn)
                    key = conn.execute(
                        select(retained_effects.c.result_chunk_key).where(
                            retained_effects.c.session_id == str(sid),
                            retained_effects.c.operation_id == running.claim.operation_id,
                            retained_effects.c.effect_key == original.owner_effect_key,
                        )
                    ).scalar_one()
                    savepoint = conn.begin_nested()
                    try:
                        body = json.loads(read_chunk(conn, session_id=str(sid), operation_id=running.claim.operation_id, key=key))
                        body["outcome"] = "blocked" if outcome == "clean" else "clean"
                        encoded = canonical_json(body).encode()
                        changed = conn.execute(
                            update(chunks)
                            .where(
                                chunks.c.session_id == str(sid),
                                chunks.c.operation_id == running.claim.operation_id,
                                chunks.c.chunk_key == key,
                            )
                            .values(payload=encoded, byte_length=len(encoded), sha256=sha256(encoded).hexdigest())
                        )
                        assert changed.rowcount == 1
                        with pytest.raises(AuditIntegrityError):
                            classify_drain_cut_on_connection(conn, running, selected, env_ref_names=names)
                    finally:
                        savepoint.rollback()
                    assert semantic_fixture._rows_on_connection(conn) == before
                if recovery_index == 0:
                    for boundary in ("suspend", "terminal"):
                        before_failure = rows()
                        corrupt_at = boundary
                        with pytest.raises(AuditIntegrityError):
                            if boundary == "suspend":
                                repository.suspend_composer_segment(running)
                            else:
                                authority.settle_continuation_owner(
                                    running, outcome=worker_lost_error(request_id=request_id), env_ref_names=names
                                )
                        assert corrupt_at is None
                        assert rows() == before_failure
                boundary = "suspend"
                receipt = await lease.consume_continuation_handoff(partial(repository.suspend_composer_segment, running))
                repository.verify_recovery(receipt)
                await lease.close()
                work.coordinator.assert_completed()
                previous = running
                (claim,) = authority.claim_segment(limit=1)
                boundary = "start"
                if recovery_index == 0:
                    before_failure = rows()
                    corrupt_at = boundary
                    with pytest.raises(AuditIntegrityError):
                        repository.start_composer_segment(
                            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
                        )
                    assert corrupt_at is None
                    assert rows() == before_failure
                context = repository.start_composer_segment(
                    claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
                )
                running = ComposerOperationRunning(claim=claim, session_operation_context=context)
                work = inventory_work(running)
                lease = await ContinuationLeaseAdoption(repository, context, lease_seconds=30, required_work=work.coordinator).join()
                stored = authority.load_continuation_snapshot(running)
                assert decode_continuation_snapshot(stored.payload_json) == selected.stack.model_copy(update={"cursor": stored.cursor})
                assert running.claim.segment_sequence == previous.claim.segment_sequence
                assert running.claim.ownership_generation == previous.claim.ownership_generation + 1
                with locked_session_transaction(engine, str(sid)) as conn:
                    with pytest.raises(ComposerOperationFenceLost):
                        semantic_sql.read_semantic_advisor_retained_inputs_on_connection(conn, previous, selected=stored)
                    inputs = semantic_sql.read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=stored)
                    assert inputs.review is not None
                    assert inputs.review.completed_checkpoint_id == original.completed.checkpoint_id
                    assert inputs.review.owner_effect_key == original.owner_effect_key
            assert tuple(case["sdk"]) == original_calls
            assert {"classify", "suspend", "start", "terminal"} <= set(observed)
            assert "poll" not in observed
        finally:
            await lease.close()
            work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_drain_refuses_unresolved_and_pending_original_work(operation_store, tmp_path, monkeypatch):
    async with semantic_fixture._semantic_case(operation_store, tmp_path, monkeypatch) as case:
        calls = []

        async def completion(**kwargs):
            calls.append(kwargs)
            return semantic_fixture._reply(json.dumps(semantic_fixture._answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)

        async def selected_at(stored):
            return await load_selected_continuation(
                authority=case["runtime"].materialization.authority,
                running=case["running"],
                selected=stored,
                required_work=case["work"],
            )

        original_admission = SessionServiceImpl.begin_v4_continuation_semantic_attempt
        awaiting_cuts = []

        async def observe_admission(self, **values):
            delivered = await original_admission(self, **values)
            assert type(delivered) is RequiredSQLReturned
            admission = delivered.value
            assert type(admission) is semantic_sql.V4SemanticProviderAdmission
            awaiting = await selected_at(admission.selected)
            assert type(awaiting.stack.frames[-1]) is AdvisorFrame
            assert awaiting.stack.frames[-1].phase == "awaiting_response"
            before = semantic_fixture._rows(operation_store[0])
            with pytest.raises(AuditIntegrityError, match="cannot replay unresolved"):
                classify_drain_cut(awaiting)
            assert semantic_fixture._rows(operation_store[0]) == before and not calls
            awaiting_cuts.append(awaiting)
            return delivered

        monkeypatch.setattr(SessionServiceImpl, "begin_v4_continuation_semantic_attempt", observe_admission)
        prepared, retained = await semantic_fixture._run_paid_semantic(case, monkeypatch)
        assert len(awaiting_cuts) == len(calls) == 1
        current = await selected_at(retained.snapshot)
        for phase in ("response_retained", "decision_ready"):
            if phase == "decision_ready":
                with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                    installed = semantic_sql.install_semantic_advisor_decision_on_connection(
                        conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
                    )
                current = await selected_at(installed)
            assert current.stack.frames[-1].phase == phase
            assert classify_drain_cut(current) == "handover"
            before = semantic_fixture._rows(operation_store[0])
            changed = replace(
                current,
                stack=current.stack.model_copy(update={"pending_settlement_ids": (retained.audit_call.call_id,)}),
            )
            with pytest.raises(AuditIntegrityError):
                classify_drain_cut(changed)
            assert semantic_fixture._rows(operation_store[0]) == before
            for disposition in ("prepared", "dispatched", "unknown"):
                changed_effects = tuple(
                    effect.model_copy(update={"disposition": disposition, "receipt_id": None})
                    if effect.effect_id == prepared.binding.effect_key
                    else effect
                    for effect in current.stack.effect_frontier
                )
                assert changed_effects != current.stack.effect_frontier
                changed = replace(current, stack=current.stack.model_copy(update={"effect_frontier": changed_effects}))
                with pytest.raises(AuditIntegrityError):
                    classify_drain_cut(changed)
                assert semantic_fixture._rows(operation_store[0]) == before
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_actual_paid_response_and_installed_decision_survive_consecutive_cold_handovers(operation_store, tmp_path, monkeypatch):
    async with semantic_fixture._semantic_case(operation_store, tmp_path, monkeypatch) as case:
        calls = []

        async def completion(**kwargs):
            calls.append(kwargs)
            return semantic_fixture._reply(json.dumps(semantic_fixture._answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await semantic_fixture._run_paid_semantic(case, monkeypatch)
        engine, repository, authority, _sessions, sid = operation_store
        running, work, lease = case["running"], case["work"], case["lease"]
        stored = retained.snapshot
        original_paid = decode_continuation_snapshot(stored.payload_json)
        try:
            for phase in ("response_retained", "decision_ready"):
                selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
                assert selected.stack.frames[-1].phase == phase
                with engine.connect() as conn:
                    assert (
                        classify_drain_cut_on_connection(conn, running, selected, env_ref_names=case["runtime"].env_ref_names) == "handover"
                    )
                receipt = await lease.consume_continuation_handoff(partial(repository.suspend_composer_segment, running))
                repository.verify_recovery(receipt)
                work.coordinator.assert_completed()
                previous = running
                fresh = new_owners((engine, repository, authority, _sessions, sid))
                engine, repository, authority, _sessions, sid = fresh
                (claim,) = authority.claim_segment(limit=1)
                context = repository.start_composer_segment(
                    claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
                )
                running = ComposerOperationRunning(claim=claim, session_operation_context=context)
                work = inventory_work(running)
                lease = await ContinuationLeaseAdoption(repository, context, lease_seconds=30, required_work=work.coordinator).join()
                stored = authority.load_continuation_snapshot(running)
                after = decode_continuation_snapshot(stored.payload_json)
                assert after == selected.stack.model_copy(update={"cursor": stored.cursor})
                assert running.claim.segment_sequence == previous.claim.segment_sequence
                assert running.claim.ownership_generation == previous.claim.ownership_generation + 1
                assert context.fence != previous.session_operation_context.fence
                with locked_session_transaction(engine, str(sid)) as conn:
                    before_read = semantic_fixture._rows_on_connection(conn)
                    with pytest.raises(ComposerOperationFenceLost):
                        semantic_sql.read_semantic_advisor_outcome_on_connection(
                            conn, previous, selected=stored, effect_key=prepared.binding.effect_key
                        )
                    assert semantic_fixture._rows_on_connection(conn) == before_read
                    if phase == "response_retained":
                        paid = semantic_sql.read_current_paid_semantic_decision_on_connection(
                            conn, running, selected=stored, effect_key=prepared.binding.effect_key
                        )
                        assert paid.paid_root == original_paid
                        assert paid.audit_call == retained.audit_call
                        stored = semantic_sql.install_semantic_advisor_decision_on_connection(
                            conn, running, selected=stored, effect_key=prepared.binding.effect_key
                        )
                    else:
                        cold = semantic_sql.read_semantic_advisor_decision_on_connection(
                            conn, running, selected=stored, effect_key=prepared.binding.effect_key
                        )
                        assert cold.decision == retained.decision
                        assert retained.response is not None
                        assert cold.source.response == retained.response.reference
            assert len(calls) == 1
            # Corrupt the actual recovered SQL source, never a supplied receipt.
            for corruption in ("event_tag", "generation", "attempt", "root"):
                with locked_session_transaction(engine, str(sid)) as conn:
                    savepoint = conn.begin_nested()
                    try:
                        if corruption == "event_tag":
                            changed = conn.execute(
                                update(events)
                                .where(
                                    events.c.session_id == str(sid),
                                    events.c.operation_id == running.claim.operation_id,
                                    events.c.sequence == stored.cursor,
                                )
                                .values(tag="checkpoint")
                            )
                        elif corruption == "attempt":
                            changed = conn.execute(
                                update(attempts)
                                .where(
                                    attempts.c.session_id == str(sid),
                                    attempts.c.operation_id == running.claim.operation_id,
                                    attempts.c.sequence == running.claim.segment_sequence,
                                    attempts.c.generation == running.claim.ownership_generation - 1,
                                )
                                .values(outcome="settled")
                            )
                        else:
                            key = f"event:{stored.cursor}" if corruption == "generation" else f"snapshot:{stored.checkpoint_id}"
                            body = read_chunk(conn, session_id=str(sid), operation_id=running.claim.operation_id, key=key).decode()
                            if corruption == "generation":
                                data = json.loads(body)
                                data["previous_generation"] += 1
                                body = canonical_json(data)
                            else:
                                root = decode_continuation_snapshot(body)
                                compose = root.frames[0].model_copy(update={"advisor_calls": root.frames[0].advisor_calls + 1})
                                body = encode_continuation_snapshot(root.model_copy(update={"frames": (compose, *root.frames[1:])}))
                            encoded = body.encode()
                            changed = conn.execute(
                                update(chunks)
                                .where(
                                    chunks.c.session_id == str(sid),
                                    chunks.c.operation_id == running.claim.operation_id,
                                    chunks.c.chunk_key == key,
                                )
                                .values(payload=encoded, byte_length=len(encoded), sha256=sha256(encoded).hexdigest())
                            )
                        assert changed.rowcount == 1
                        actual = semantic_sql._historical_snapshot(
                            conn, str(sid), running.claim.operation_id, checkpoint_id=stored.checkpoint_id
                        )
                        with pytest.raises(AuditIntegrityError):
                            semantic_sql.read_semantic_advisor_decision_on_connection(
                                conn, running, selected=actual, effect_key=prepared.binding.effect_key
                            )
                    finally:
                        savepoint.rollback()
                    assert (
                        semantic_sql.read_semantic_advisor_decision_on_connection(
                            conn, running, selected=stored, effect_key=prepared.binding.effect_key
                        ).decision
                        == retained.decision
                    )
            assert len(calls) == 1
        finally:
            await lease.close()
            work.coordinator.assert_completed()


@asynccontextmanager
async def _actual_iteration_stop_case(tmp_path, monkeypatch, kind):
    if kind == "owned":
        async with actual_owned_recovery_case(tmp_path, monkeypatch) as case:
            harness, sdk, operation, reply, _arguments, _inputs = case
            yield harness, sdk, operation, reply
        return
    operation = str(uuid4())
    reply = "The draft now has its requested name."

    def stop(request):
        answer = semantic_primary_response(request)
        retained = json.loads(request["messages"][-1]["content"])
        returned = retained["iteration_return"]
        assert returned["schema_version"] == "composer.continuation-result.v1"
        frontier = decode_continuation_frontier(retained["frontier"])
        answer.update(
            decision="STOP_CONFIRMED",
            remaining_actionable_ids=[],
            prescribed_action=None,
            obligation_ids=[item.obligation_id for item in frontier.obligations],
            obligations=[
                {
                    "obligation_id": item.obligation_id,
                    "parent_id": item.parent_id,
                    "description": item.description,
                    "original_message_id": item.original_message_id,
                    "start": item.start,
                    "end": item.end,
                    "status": "satisfied",
                }
                for item in frontier.obligations
            ],
            reason="The actual retained reviewed result completes the named-draft request.",
        )
        return answer

    app_ref = []
    sdk = ScriptedSDK(
        [
            (
                "primary",
                lambda app: ("Naming the draft.", [tool_call(app, "name-draft", "set_metadata", {"patch": {"name": "Named draft"}})]),
            ),
            ("primary", (reply, None)),
            ("end", end_admit()),
            ("semantic", stop),
        ],
        app_ref=app_ref,
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch, composer_max_composition_turns=1) as harness:
        app_ref.append(harness.app)
        yield harness, sdk, operation, reply


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("clean", "owned"))
async def test_applied_iteration_paid_stop_cold_handover_publishes_original_result(tmp_path, monkeypatch, kind):
    async with _actual_iteration_stop_case(tmp_path, monkeypatch, kind) as (harness, sdk, operation, reply):
        actual_advance = driver_module._SegmentDriver.advance
        actual_dispose = driver_module._SegmentDriver.dispose
        actual_fit = driver_module.admit_action
        actual_init = driver_module._SegmentDriver.__init__
        before, after, handed_over, original_effects = [], [], [], []
        cold_owners = []
        forced = False

        def observe_init(driver, *args, **kwargs):
            actual_init(driver, *args, **kwargs)
            if (
                driver.running.claim.operation_id == operation
                and before
                and driver.running.claim.ownership_generation == before[0][0].ownership_generation + 1
            ):
                cold_owners.append(driver.phases.runtime is None)

        async def observe_advance(driver, stored, selected):
            if driver.running.claim.operation_id != operation:
                return await actual_advance(driver, stored, selected)
            if before and driver.running.claim.ownership_generation == before[0][0].ownership_generation + 1 and not after:
                after.append(selected)
            result = await actual_advance(driver, stored, selected)
            if result is None and not before:
                following, current = await driver.selected()
                if type(current.stack.frames[-1]) is ToolBatchFrame:
                    reader = (
                        semantic_sql._semantic_iteration_publication_data
                        if kind == "clean"
                        else semantic_sql._semantic_owned_iteration_publication_data
                    )
                    with harness.app.state.session_engine.connect() as conn:
                        proof = reader(conn, str(harness.session_id), operation, following)
                    if proof is not None:
                        before.append((driver.running.claim, current))
                        original_effects.extend(effects(harness.app, operation))
            return result

        async def observe_dispose(driver, original=None):
            result = await actual_dispose(driver, original)
            if driver.running.claim.operation_id == operation:
                handed_over.append(result)
            return result

        def one_handover(*args, **kwargs):
            nonlocal forced
            if before and not forced:
                forced = True
                return SegmentFit.YIELD
            return actual_fit(*args, **kwargs)

        monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
        monkeypatch.setattr(driver_module._SegmentDriver, "__init__", observe_init)
        monkeypatch.setattr(driver_module._SegmentDriver, "dispose", observe_dispose)
        monkeypatch.setattr(driver_module, "admit_action", one_handover)
        request = (
            "Name the draft 'Named recovery draft', then park it and finish this request."
            if kind == "owned"
            else "Name the draft 'Named draft' and finish this request."
        )
        terminal = await submit_and_settle(harness, message_body(request, operation_id=operation))
        assert terminal.status == "completed", terminal.error
        assert terminal.result is not None and terminal.result.message.content.split("\n\n---\n\n")[0] == reply
        assert len(before) == len(after) == len(handed_over) == 1 and forced
        assert cold_owners == [True]
        assert type(handed_over[0]) is driver_module.ContinuationSegmentHandedOver
        receipt = handed_over[0].receipt
        assert receipt.sequence == before[0][0].segment_sequence
        assert receipt.generation == before[0][0].ownership_generation + 1
        assert after[0].stack == before[0][1].stack.model_copy(update={"cursor": after[0].stack.cursor})
        final_effects = {row.effect_key: row for row in effects(harness.app, operation)}
        assert all(final_effects[row.effect_key] == row for row in original_effects)
        assert sdk.roles == (["primary", "primary", "semantic"] if kind == "owned" else ["primary", "primary", "end", "semantic"])
        assert not sdk.steps


@pytest.mark.asyncio
async def test_consumed_paid_stop_handover_reproves_original_source_at_actual_final_publication(operation_store, tmp_path, monkeypatch):
    async with actual_semantic_publication_case(operation_store, tmp_path, monkeypatch) as case:
        engine, repository, _authority, _sessions, _sid = operation_store
        original_running, original = case["running"], case["publication_result"]
        selected = await load_selected_continuation(
            authority=case["phases"].authority,
            running=original_running,
            selected=case["publication"],
            required_work=case["work"],
        )
        with engine.connect() as conn:
            assert (
                classify_drain_cut_on_connection(conn, original_running, selected, env_ref_names=case["execution"].runtime.env_ref_names)
                == "handover"
            )
        receipt = await case["lease"].consume_continuation_handoff(partial(repository.suspend_composer_segment, original_running))
        repository.verify_recovery(receipt)
        fresh = new_owners(operation_store)
        engine, repository, authority, sessions, _sid = fresh
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        running = ComposerOperationRunning(claim=claim, session_operation_context=context)
        work = inventory_work(running)
        lease = await ContinuationLeaseAdoption(repository, context, lease_seconds=30, required_work=work.coordinator).join()

        async def forbidden_sdk(**_kwargs):
            raise AssertionError("cold Stop publication replayed paid provider work")

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        try:
            stored = authority.load_continuation_snapshot(running)
            assert decode_continuation_snapshot(stored.payload_json) == selected.stack.model_copy(update={"cursor": stored.cursor})
            with engine.connect() as conn:
                with pytest.raises(ComposerOperationFenceLost):
                    require_semantic_publication_on_connection(conn, original_running, selected=stored)
                proof = require_semantic_publication_on_connection(conn, running, selected=stored)
                assert proof is not None
                assert proof.admission.paid.audit_call == case["publication_proof"].admission.paid.audit_call
                assert proof.candidate == case["publication_proof"].candidate
                assert proof.review_result == case["publication_proof"].review_result
            draft = composer_turn_end_assistant_row(original)
            terminal = await sessions.complete_composer_async_operation(
                running,
                assistant=ComposerOperationAssistantWrite(
                    message_id=uuid4(),
                    content=draft.content,
                    raw_content=draft.raw_content,
                    composition_state_id=None if original.final_persisted_state_id is None else UUID(original.final_persisted_state_id),
                ),
                assistant_record=None,
                audit_cohort=(),
                audit_composition_state_id=None,
                required_work=work.coordinator,
                provider_audit_calls=original.llm_calls,
                publication_result=original,
                build_response=partial(_message_with_state_response, state_response=None, live_validation=None),
            )
            assert terminal.status == "completed" and terminal.result.message.content == draft.content
            assert running.claim.segment_sequence == original_running.claim.segment_sequence
            assert running.claim.ownership_generation == original_running.claim.ownership_generation + 1
        finally:
            await lease.close()
            work.coordinator.assert_completed()


@asynccontextmanager
async def _actual_completed_source_case(tmp_path, monkeypatch, cut):
    if cut.startswith("iteration_"):
        async with _actual_iteration_stop_case(tmp_path, monkeypatch, "clean") as case:
            harness, sdk, operation, reply = case
            if cut in ("iteration_read_committed", "iteration_read_applied"):
                sdk.steps.insert(-1, ("semantic", semantic_read_response))
            yield harness, sdk, operation, reply
        return
    operation = str(uuid4())
    reply = "The original request is complete."
    if cut == "ordinary_recovery_stop":
        script = [
            (
                "primary",
                (
                    None,
                    [
                        {
                            "id": "original-park",
                            "type": "function",
                            "function": {"name": "handoff_to_user", "arguments": canonical_json({"action": "park", "message": reply})},
                        }
                    ],
                ),
            ),
            ("semantic", lambda request: _ordinary_recovery_stop(request, reply=reply)),
        ]
    else:
        script = [
            ("primary", (reply, None)),
            ("semantic", semantic_read_response),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    sdk = ScriptedSDK(script)
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app_ref.append(harness.app)
        yield harness, sdk, operation, reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cut", "recoveries"),
    (
        ("read_committed", 1),
        ("read_committed", 2),
        ("iteration_returned", 1),
        ("iteration_read_committed", 1),
        ("iteration_read_committed", 2),
        ("iteration_read_applied", 1),
        ("ordinary_recovery_stop", 1),
        ("ordinary_recovery_stop", 2),
    ),
)
async def test_original_driver_restores_completed_sources_before_next_action(tmp_path, monkeypatch, cut, recoveries):
    async with _actual_completed_source_case(tmp_path, monkeypatch, cut) as (harness, sdk, operation, reply):
        original_advance, original_fit = driver_module._SegmentDriver.advance, driver_module.admit_action
        before, after, paid_effects, applied_reads = [], [], [], []
        forced = 0

        async def observe_advance(driver, stored, selected):
            if driver.running.claim.operation_id != operation:
                return await original_advance(driver, stored, selected)
            if before and not after and driver.running.claim.ownership_generation == before[0][0].ownership_generation + recoveries:
                after.append(selected)
                assert driver.phases.runtime is None
            result = await original_advance(driver, stored, selected)
            if result is None and (not before or (cut == "iteration_read_committed" and not applied_reads)):
                following, current = await driver.selected()
                with harness.app.state.session_engine.connect() as conn:
                    event = conn.execute(
                        select(events).where(events.c.operation_id == operation, events.c.sequence == following.cursor)
                    ).one()
                    body = json.loads(
                        read_chunk(conn, session_id=str(harness.session_id), operation_id=operation, key=event.payload_chunk_key)
                    )
                if (
                    cut == "iteration_read_committed"
                    and before
                    and event.tag == "checkpoint"
                    and body.get("kind") == "semantic_read_applied"
                ):
                    assert type(current.stack.frames[-1]) is ToolBatchFrame
                    applied_reads.append(
                        (following, ContinuationEvidenceRef.model_validate_json(canonical_json(body["reference"]), strict=True))
                    )
                reached = event.tag == "checkpoint" and (
                    (cut == "read_committed" and body.get("kind") == "semantic_read_committed")
                    or (
                        cut == "iteration_read_committed"
                        and body.get("kind") == "semantic_read_committed"
                        and tuple(type(frame) for frame in current.stack.frames) == (ComposeFrame, ToolBatchFrame, AdvisorFrame)
                    )
                    or (cut == "iteration_returned" and body.get("transition") == "iteration_stage" and body.get("stage") == "returned")
                    or (
                        cut == "iteration_read_applied"
                        and body.get("kind") == "semantic_read_applied"
                        and type(current.stack.frames[-1]) is ToolBatchFrame
                    )
                    or (cut == "ordinary_recovery_stop" and body.get("kind") == "semantic_ordinary_recovery_stop_ready")
                )
                if reached and not before:
                    before.append((driver.running.claim, current, following))
                    paid_effects.extend(effects(harness.app, operation))
            return result

        def original_cut_handover(*args, **kwargs):
            nonlocal forced
            if before and forced < recoveries:
                forced += 1
                return SegmentFit.YIELD
            return original_fit(*args, **kwargs)

        monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
        monkeypatch.setattr(driver_module, "admit_action", original_cut_handover)
        trace = observe_original_driver_failures(monkeypatch, operation, session_id=harness.session_id)
        request = (
            "Name the draft 'Named draft' and finish this request."
            if cut.startswith("iteration_")
            else "Park the draft and finish this request."
            if cut == "ordinary_recovery_stop"
            else "Explain this pipeline."
        )
        try:
            terminal = await submit_and_settle(harness, message_body(request, operation_id=operation), rounds=720)
        finally:
            (tmp_path / f"recovery-{cut}-{recoveries}.json").write_text(json.dumps(trace, sort_keys=True), encoding="utf-8")
        assert terminal.status == "completed", terminal.error
        assert terminal.result is not None and terminal.result.message.content.split("\n\n---\n\n")[0] == reply
        assert len(before) == len(after) == 1 and forced == recoveries
        assert after[0].stack == before[0][1].stack.model_copy(update={"cursor": after[0].stack.cursor})
        assert after[0].stack.cursor == before[0][1].stack.cursor + recoveries
        final_effects = {row.effect_key: row for row in effects(harness.app, operation)}
        assert all(final_effects[row.effect_key] == row for row in paid_effects)
        expected = (
            ["primary", "semantic"]
            if cut == "ordinary_recovery_stop"
            else ["primary", "primary", "end", "semantic"]
            if cut == "iteration_returned"
            else ["primary", "primary", "end", "semantic", "semantic"]
            if cut in ("iteration_read_committed", "iteration_read_applied")
            else ["primary", "semantic", "semantic", "end", "semantic"]
        )
        assert sdk.roles == expected and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            if cut == "iteration_read_committed":
                assert len(applied_reads) == 1
                applied, reference = applied_reads[0]
                _value, bridge = semantic_sql._read_input_with_bridge_data(conn, str(harness.session_id), operation, reference)
                assert bridge.commit_checkpoint_id == before[0][2].checkpoint_id
                assert bridge.commit_cursor == before[0][2].cursor
                assert bridge.consume_cursor == applied.cursor == after[0].stack.cursor + 1
                assert semantic_sql._returned_iteration_source_data(conn, str(harness.session_id), operation, applied) is not None
            if cut == "ordinary_recovery_stop":
                original = semantic_sql._semantic_ordinary_recovery_publication_data(conn, str(harness.session_id), operation, before[0][2])
                assert original is not None
                recovered = semantic_sql._historical_snapshot(
                    conn, str(harness.session_id), operation, checkpoint_id=after[0].checkpoint_id
                )
                assert (
                    semantic_sql._semantic_ordinary_recovery_publication_data(conn, str(harness.session_id), operation, recovered)
                    == original
                )
                for corruption in ("event", "attempt"):
                    savepoint = conn.begin_nested()
                    try:
                        if corruption == "event":
                            changed = conn.execute(
                                update(events)
                                .where(events.c.operation_id == operation, events.c.sequence == recovered.cursor)
                                .values(tag="checkpoint")
                            )
                        else:
                            changed = conn.execute(
                                update(attempts)
                                .where(
                                    attempts.c.operation_id == operation,
                                    attempts.c.sequence == before[0][0].segment_sequence,
                                    attempts.c.generation == before[0][0].ownership_generation,
                                )
                                .values(outcome="settled")
                            )
                        assert changed.rowcount == 1
                        with pytest.raises(AuditIntegrityError):
                            semantic_sql._semantic_ordinary_recovery_publication_data(conn, str(harness.session_id), operation, recovered)
                    finally:
                        savepoint.rollback()
                    assert (
                        semantic_sql._semantic_ordinary_recovery_publication_data(conn, str(harness.session_id), operation, recovered)
                        == original
                    )
            if cut in ("read_committed", "iteration_read_committed", "iteration_read_applied"):
                document = json.loads(sdk.requests[-1]["messages"][-1]["content"])
                assert document["read_results"]
