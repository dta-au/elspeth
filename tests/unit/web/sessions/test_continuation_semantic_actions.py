"""Paid K-7 read/Stop actions through current SQL and the real read owner."""

from __future__ import annotations

import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from functools import partial
from hashlib import sha256
from uuid import UUID

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.composer.continuation_end_values import continuation_candidate_id, decode_end_candidate
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_progress import ContinuationProtocolError
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor import prepare_semantic_advisor_request
from elspeth.web.composer.continuation_semantic_advisor_evidence import derive_paid_semantic_evidence
from elspeth.web.composer.continuation_semantic_advisor_read import execute_semantic_advisor_read
from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation
from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions import continuation_semantic_actions as action_sql
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationFenceLost,
    ComposerOperationRunning,
)
from elspeth.web.sessions.continuation import ComposerSegmentStartObservation, ContinuationCASConflict, load_snapshot, read_chunk
from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result
from elspeth.web.sessions.continuation_semantic_actions import (
    install_semantic_action_on_connection,
    prepare_semantic_read_on_connection,
    read_committed_semantic_read_on_connection,
    read_original_committed_semantic_read_on_connection,
    read_semantic_action_admission_on_connection,
    require_installed_semantic_action_on_connection,
    require_semantic_read_execution_on_connection,
)
from elspeth.web.sessions.continuation_semantic_advisor import (
    consume_committed_semantic_read_on_connection,
    read_current_paid_semantic_decision_on_connection,
    read_semantic_advisor_retained_inputs_on_connection,
)
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _reply
from tests.unit.web.sessions import test_continuation_semantic_publication as publication_fixture
from tests.unit.web.sessions.test_continuation_semantic_advisor import _rows_on_connection, _run_paid_semantic, _semantic_case
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


@asynccontextmanager
async def _paid_install_case(store, tmp_path, monkeypatch, kind):
    async with _semantic_case(store, tmp_path, monkeypatch, end_candidate=kind == "review") as case:
        answer = _read_answer(case["request"]) if kind == "read" else _answer(case["request"])
        if kind == "review":
            selected = case["runtime"].source.selected
            end = selected.stack.frames[1]
            assert type(end) is EndFrame
            candidate = decode_end_candidate(next(chunk for chunk in selected.chunks if chunk.reference == end.candidate))
            action = answer["prescribed_action"]
            del action["instruction"]
            action.update(
                kind="review",
                candidate_ref=continuation_candidate_id(candidate),
                evidence_binding=case["request"].source.coverage_binding,
            )
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(answer))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, paid = await _run_paid_semantic(case, monkeypatch)
        assert len(sdk) == 1 and paid.audit_call.provider_cost == 0.01
        yield case, paid.snapshot, prepared.binding.effect_key
        assert len(sdk) == 1


def _prove_paid_install_equivalence(conn, running, selected, effect_key, monkeypatch):
    """Use the same real paid SQL source for both writers, restoring every probe."""
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    before = _rows_on_connection(conn)
    actual_paid = semantic_sql._paid_result_data
    reads = []

    def paid_read(connection, session_id, operation_id, snapshot, root, key):
        if (session_id, operation_id, key) == (sid, operation, effect_key):
            reads.append((connection, snapshot.checkpoint_id))
        return actual_paid(connection, session_id, operation_id, snapshot, root, key)

    def installation_bytes(installed):
        appended = conn.execute(
            select(events.c.sequence, events.c.tag, events.c.payload_chunk_key)
            .where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence > selected.cursor)
            .order_by(events.c.sequence)
        ).all()
        assert len(appended) == 1 and installed.cursor == selected.cursor + 1
        return (
            installed.payload_json,
            installed.cursor,
            tuple(
                (row.sequence, row.tag, read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key))
                for row in appended
            ),
        )

    with monkeypatch.context() as capture:
        capture.setattr(semantic_sql, "_paid_result_data", paid_read)
        capture.setattr(action_sql, "_paid_result_data", paid_read)
        savepoint = conn.begin_nested()
        try:
            paid = read_current_paid_semantic_decision_on_connection(conn, running, selected=selected, effect_key=effect_key)
            action = paid.decision.prescribed_action
            primary = action is not None and action.kind == "primary_turn"
            original_install = (
                semantic_sql.install_semantic_advisor_decision_on_connection if primary else install_semantic_action_on_connection
            )
            original = original_install(conn, running, selected=selected, effect_key=effect_key)
            expected = installation_bytes(original)
            assert reads == [(conn, selected.checkpoint_id)] * 2
            ledger_entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=paid.audit_call.call_id)
        finally:
            savepoint.rollback()
        assert _rows_on_connection(conn) == before

        # A changed paid source must fail before any installation writes.
        savepoint = conn.begin_nested()
        try:
            changed = conn.execute(update(usage).where(usage.c.entry_id == ledger_entry_id).values(prompt_tokens=999))
            assert changed.rowcount == 1
            damaged = _rows_on_connection(conn)
            with pytest.raises(AuditIntegrityError, match="ledger"):
                semantic_sql.install_current_semantic_decision_on_connection(conn, running, selected=selected, effect_key=effect_key)
            assert _rows_on_connection(conn) == damaged
        finally:
            savepoint.rollback()
        assert _rows_on_connection(conn) == before

        reads.clear()
        savepoint = conn.begin_nested()
        try:
            installed = semantic_sql.install_current_semantic_decision_on_connection(
                conn, running, selected=selected, effect_key=effect_key
            )
            assert installation_bytes(installed) == expected
            assert reads == [(conn, selected.checkpoint_id)]
            current_reader = (
                semantic_sql.read_semantic_advisor_decision_on_connection if primary else require_installed_semantic_action_on_connection
            )
            current = current_reader(conn, running, selected=installed, effect_key=effect_key)
            assert reads == [(conn, selected.checkpoint_id), (conn, installed.checkpoint_id)]
            installed_rows = _rows_on_connection(conn)
            corruption = conn.begin_nested()
            try:
                changed = conn.execute(update(usage).where(usage.c.entry_id == ledger_entry_id).values(prompt_tokens=999))
                assert changed.rowcount == 1
                damaged = _rows_on_connection(conn)
                with pytest.raises(AuditIntegrityError, match="ledger"):
                    current_reader(conn, running, selected=installed, effect_key=effect_key)
                assert _rows_on_connection(conn) == damaged
            finally:
                corruption.rollback()
            assert _rows_on_connection(conn) == installed_rows
            assert current_reader(conn, running, selected=installed, effect_key=effect_key) == current
        finally:
            savepoint.rollback()
        assert _rows_on_connection(conn) == before
    return semantic_sql.install_current_semantic_decision_on_connection(conn, running, selected=selected, effect_key=effect_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("primary", "read", "review"))
async def test_current_paid_install_matches_original_writes_and_reproves_changed_root(operation_store, tmp_path, monkeypatch, kind):
    async with _paid_install_case(operation_store, tmp_path, monkeypatch, kind) as (case, selected, effect_key):
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            installed = _prove_paid_install_equivalence(conn, case["running"], selected, effect_key, monkeypatch)
            assert installed.cursor == selected.cursor + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("read", "review"))
async def test_paid_source_reuses_actual_recorder_and_preserves_standalone_extension(operation_store, tmp_path, monkeypatch, kind):
    from elspeth.web.composer import continuation_evidence_application_data as application_data

    async with _paid_install_case(operation_store, tmp_path, monkeypatch, kind) as (case, selected, effect_key):
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            original_decode = semantic_sql.decode_recorder_snapshot
            original_extend = semantic_sql._extend_admitted_evidence
            original_compare = application_data.require_retained_call_extension
            decoder_inputs, decoded, extensions, comparisons = [], [], [], []

            def decode(chunk):
                decoder_inputs.append(chunk)
                snapshot = original_decode(chunk)
                decoded.append((chunk, snapshot))
                return snapshot

            def extend(data, original, admitted, call, snapshot):
                result = original_extend(data, original, admitted, call, snapshot)
                extensions.append((data, original, admitted, call, snapshot, result))
                return result

            def compare(admitted, *, call, recorder, retained_evidence, env_ref_names):
                result = original_compare(
                    admitted, call=call, recorder=recorder, retained_evidence=retained_evidence, env_ref_names=env_ref_names
                )
                comparisons.append((admitted, call, recorder, retained_evidence, env_ref_names, result))
                return result

            monkeypatch.setattr(semantic_sql, "decode_recorder_snapshot", decode)
            monkeypatch.setattr(semantic_sql, "_extend_admitted_evidence", extend)
            monkeypatch.setattr(application_data, "require_retained_call_extension", compare)
            before = _rows_on_connection(conn)
            running = case["running"]
            root = decode_continuation_snapshot(selected.payload_json)
            actual = semantic_sql._paid_result_data(
                conn, str(running.claim.session_id), running.claim.operation_id, selected, root, effect_key
            )
            result, data = actual.result, actual.invocation
            assert len(comparisons) == 1 and extensions == []
            admitted, call, snapshot, retained_evidence, names, expected = comparisons[0]
            original = decode_continuation_snapshot(data.source.selected_snapshot_json)
            assert admitted.source_chunk == data.source.evidence and names == data.source.env_ref_names
            assert call is result.audit_call and snapshot.llm_calls[-1] is call
            assert expected.evidence is retained_evidence and expected.recorder == result.recorder
            assert retained_evidence.reference == semantic_sql._parent(root).evidence
            assert (
                semantic_sql._ref(conn, str(running.claim.session_id), running.claim.operation_id, retained_evidence.reference)
                == retained_evidence
            )
            matching = [(chunk, value) for chunk, value in decoded if chunk == result.recorder]
            assert len(matching) == 1
            assert matching[0][0] is result.recorder and matching[0][1] is snapshot
            assert _rows_on_connection(conn) == before

            # The standalone wrapper still decodes the exact supplied bytes,
            # and normalizes an equal-valued call to its decoder's actual atom.
            equal_call = replace(call)
            assert equal_call == call and equal_call is not call
            standalone = semantic_sql._extended_evidence_data(
                conn, str(running.claim.session_id), running.claim.operation_id, data, equal_call, result.recorder
            )
            assert standalone.evidence == expected.evidence and standalone.applied_batch is None
            assert len(extensions) == 1 and extensions[0][0] is data and extensions[0][5] is standalone
            matching = [(chunk, value) for chunk, value in decoded if chunk == result.recorder]
            assert len(matching) == 2 and all(chunk is result.recorder for chunk, _value in matching)
            assert matching[-1][1] is extensions[-1][4] and matching[-1][1] is not snapshot

            wrong_call = replace(call, call_id="other-original-paid-call")
            with pytest.raises(AuditIntegrityError, match="one original physical call"):
                semantic_sql._extended_evidence_data(
                    conn, str(running.claim.session_id), running.claim.operation_id, data, wrong_call, result.recorder
                )
            wrong_prefix = replace(snapshot, llm_calls=(call, *snapshot.llm_calls))
            for compared_call, compared_recorder in ((equal_call, snapshot), (call, wrong_prefix)):
                with pytest.raises(AuditIntegrityError, match="exact original recorder prefix"):
                    original_compare(
                        admitted,
                        call=compared_call,
                        recorder=compared_recorder,
                        retained_evidence=retained_evidence,
                        env_ref_names=names,
                    )
            with pytest.raises(AuditIntegrityError):
                original_compare(
                    admitted,
                    call=call,
                    recorder=snapshot,
                    retained_evidence=PrivateChunk(retained_evidence.reference, "{"),
                    env_ref_names=names,
                )
            with pytest.raises(AuditIntegrityError, match="exact original recorder prefix"):
                original_extend(data, original, admitted, call, wrong_prefix)
            with pytest.raises(AuditIntegrityError, match="exact original recorder prefix"):
                semantic_sql.append_primary_call_to_evidence(
                    admitted, call=equal_call, recorder=snapshot, env_ref_names=data.source.env_ref_names
                )

            # Both inputs are corrupt: original evidence admission must still
            # refuse first, without even entering the recorder decoder.
            broken_evidence = PrivateChunk(data.source.evidence.reference, "{")
            broken_data = replace(data, source=replace(data.source, evidence=broken_evidence))
            broken_recorder = PrivateChunk(result.recorder.reference, "{")
            decoded_before = len(decoder_inputs)
            with pytest.raises(AuditIntegrityError, match="evidence source exceeds its original complete document bound"):
                semantic_sql._extended_evidence_data(
                    conn, str(running.claim.session_id), running.claim.operation_id, broken_data, call, broken_recorder
                )
            assert len(decoder_inputs) == decoded_before
            with pytest.raises(AuditIntegrityError):
                semantic_sql._extended_evidence_data(
                    conn, str(running.claim.session_id), running.claim.operation_id, data, call, broken_recorder
                )
            assert len(decoder_inputs) == decoded_before + 1 and decoder_inputs[-1] is broken_recorder
            assert _rows_on_connection(conn) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("read", "review"))
async def test_installed_source_reuses_actual_derivation_and_refuses_substituted_invocation(operation_store, tmp_path, monkeypatch, kind):
    from elspeth.web.composer import continuation_semantic_invocation as invocation_module

    async with _paid_install_case(operation_store, tmp_path, monkeypatch, kind) as (case, selected, effect_key):
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            running = case["running"]
            installed = install_semantic_action_on_connection(conn, running, selected=selected, effect_key=effect_key)
            original_derive = invocation_module._derive
            original_pair = action_sql._original_paid_for_selected
            sources, pairs = [], []

            def derive(source):
                sources.append(source)
                return original_derive(source)

            def paired(connection, session_id, operation_id, snapshot, root, key):
                result = original_pair(connection, session_id, operation_id, snapshot, root, key)
                if snapshot.checkpoint_id == installed.checkpoint_id and key == effect_key:
                    pairs.append(result)
                return result

            monkeypatch.setattr(invocation_module, "_derive", derive)
            monkeypatch.setattr(action_sql, "_original_paid_for_selected", paired)
            before = _rows_on_connection(conn)
            admitted = require_installed_semantic_action_on_connection(conn, running, selected=installed, effect_key=effect_key)
            assert len(pairs) == 1
            paid, original, primary_response = pairs[0]
            assert paid.invocation is original and admitted.paid is paid
            assert admitted.original_primary_response is primary_response
            if kind == "review":
                assert primary_response is not None
                assert primary_response.source_checkpoint_id == original.source.checkpoint_id
                assert primary_response.source_cursor == original.source.cursor
            else:
                assert primary_response is None
            assert sum(source is original.source for source in sources) == 1
            assert _rows_on_connection(conn) == before

            # Positive observer control: standalone verification still rederives.
            original.verify(original.source)
            assert sum(source is original.source for source in sources) == 2
            independently_verified = action_sql._admit_paid(conn, paid)
            assert independently_verified.original_primary_response is None
            assert replace(independently_verified, original_primary_response=primary_response) == admitted
            assert sum(source is original.source for source in sources) == 3
            assert _rows_on_connection(conn) == before

            for change in ("derived", "equal_copy", "original_copy"):

                def substituted(connection, session_id, operation_id, snapshot, root, key, *, change=change):
                    actual_paid, actual_original, actual_primary = original_pair(connection, session_id, operation_id, snapshot, root, key)
                    if snapshot.checkpoint_id != installed.checkpoint_id or key != effect_key:
                        return actual_paid, actual_original, actual_primary
                    if change == "original_copy":
                        return actual_paid, replace(actual_original), actual_primary
                    replacement = replace(actual_original)
                    if change == "derived":
                        replacement = replace(
                            actual_original,
                            invocation=actual_original.invocation.model_copy(update={"coverage_binding": stable_hash("other")}),
                        )
                    else:
                        assert replacement == actual_original and replacement is not actual_original
                    return replace(actual_paid, invocation=replacement), actual_original, actual_primary

                monkeypatch.setattr(action_sql, "_original_paid_for_selected", substituted)
                with pytest.raises(AuditIntegrityError, match="substituted its original derived invocation"):
                    require_installed_semantic_action_on_connection(conn, running, selected=installed, effect_key=effect_key)
                assert _rows_on_connection(conn) == before

            # The public evidence API continues refusing a nominal but inconsistent DTO.
            changed = replace(
                paid,
                invocation=replace(original, invocation=original.invocation.model_copy(update={"coverage_binding": stable_hash("other")})),
            )
            with pytest.raises(AuditIntegrityError, match="derived identities"):
                derive_paid_semantic_evidence(changed, end_candidate=action_sql._end_candidate(conn, paid))
            assert _rows_on_connection(conn) == before


@pytest.mark.asyncio
async def test_current_paid_install_whole_stop_preserves_real_review_publication(operation_store, tmp_path, monkeypatch):
    installations = []

    def install(conn, running, *, selected, effect_key):
        installed = _prove_paid_install_equivalence(conn, running, selected, effect_key, monkeypatch)
        root = decode_continuation_snapshot(installed.payload_json)
        assert root.active_action_json is None
        installations.append(installed)
        return installed

    monkeypatch.setattr(publication_fixture, "install_semantic_action_on_connection", install)
    async with publication_fixture.actual_semantic_publication_case(operation_store, tmp_path, monkeypatch) as case:
        assert len(installations) == 1
        proof = case["publication_proof"]
        assert proof.admission.paid.decision.decision == "STOP_CONFIRMED"
        assert proof.admission.paid.decision.prescribed_action is None
        assert case["publication"].cursor > installations[0].cursor


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", (False, True), ids=("standalone", "end-router"))
async def test_actual_end_stop_reuses_only_immediate_admission_and_reproves_changed_root(operation_store, tmp_path, monkeypatch, routed):
    reached = []
    original_require = action_sql.require_semantic_whole_stop_on_connection
    original_append = semantic_sql.append_complete_snapshot
    original_publication = action_sql.require_semantic_publication_on_connection
    consume = (
        semantic_sql.consume_semantic_advisor_end_stop_on_connection
        if routed
        else semantic_sql.consume_semantic_advisor_whole_stop_on_connection
    )

    def consume_actual(conn, running, *, selected, effect_key):
        before = _rows_on_connection(conn)
        with pytest.raises(ContinuationCASConflict, match="substituted its complete selected root"):
            consume(conn, running, selected=replace(selected, payload_json="{}"), effect_key=effect_key)
        assert _rows_on_connection(conn) == before
        reads, appends, publications = [], [], []

        def require_actual(connection, owner, *, selected, effect_key):
            admission = original_require(connection, owner, selected=selected, effect_key=effect_key)
            if not appends:
                assert connection is conn and owner is running
                reads.append(admission)
                original_rows = _rows_on_connection(conn)
                root = decode_continuation_snapshot(selected.payload_json)
                for changed in (
                    replace(admission, paid=replace(admission.paid, selected=replace(selected, cursor=selected.cursor + 1))),
                    replace(
                        admission,
                        paid=replace(admission.paid, source=admission.paid.source.model_copy(update={"effect_key": effect_key + ":other"})),
                    ),
                ):
                    with pytest.raises(AuditIntegrityError, match="locally admitted current source"):
                        semantic_sql._consume_semantic_whole_stop_from_admission(
                            conn, running, selected=selected, effect_key=effect_key, root=root, admission=changed
                        )
                    assert _rows_on_connection(conn) == original_rows
            return admission

        def append_actual(connection, **kwargs):
            assert connection is conn and kwargs["expected_checkpoint_id"] == selected.checkpoint_id
            # Count only the full proof before this actual root write. Later
            # publication/history admissions remain required and unbounded here.
            assert len(reads) == 1
            appended = original_append(connection, **kwargs)
            appends.append(appended)
            return appended

        def publication_actual(connection, owner, *, selected):
            assert connection is conn and owner is running and appends == [selected]
            original_rows = _rows_on_connection(conn)
            with pytest.raises(ContinuationCASConflict, match="changed its complete selected root"):
                original_publication(conn, running, selected=replace(selected, payload_json="{}"))
            assert _rows_on_connection(conn) == original_rows
            proof = original_publication(connection, owner, selected=selected)
            assert proof is not None
            publications.append(proof)
            return proof

        with monkeypatch.context() as capture:
            capture.setattr(action_sql, "require_semantic_whole_stop_on_connection", require_actual)
            capture.setattr(semantic_sql, "append_complete_snapshot", append_actual)
            capture.setattr(action_sql, "require_semantic_publication_on_connection", publication_actual)
            result = consume(conn, running, selected=selected, effect_key=effect_key)
        assert len(reads) == len(appends) == len(publications) == 1
        assert result == appends[0] and result.cursor == selected.cursor + 1
        reached.append((selected, result, publications[0]))
        return result

    monkeypatch.setattr(publication_fixture, "consume_semantic_advisor_whole_stop_on_connection", consume_actual)
    async with publication_fixture.actual_semantic_publication_case(operation_store, tmp_path, monkeypatch) as case:
        assert len(reached) == 1
        installed, consumed, proof = reached[0]
        assert case["publication"] == consumed and installed.cursor + 1 == consumed.cursor
        assert case["publication_proof"] == proof
        assert proof.admission.paid.decision.decision == "STOP_CONFIRMED"


@pytest.mark.asyncio
async def test_current_paid_install_rechecks_actual_stop_before_write(operation_store, tmp_path, monkeypatch):
    async with _paid_install_case(operation_store, tmp_path, monkeypatch, "read") as (case, selected, effect_key):
        running = case["running"]
        stopped = case["runtime"].materialization.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            before = _rows_on_connection(conn)
            with pytest.raises(ComposerOperationCancelledDuringTurn):
                semantic_sql.install_current_semantic_decision_on_connection(conn, running, selected=selected, effect_key=effect_key)
            assert _rows_on_connection(conn) == before


def _read_answer(request):
    answer = _answer(request)
    action = answer["prescribed_action"]
    del action["instruction"]
    action.update(
        kind="read_tool",
        tool_name="get_plugin_schema",
        arguments_json=canonical_json({"plugin_type": "source", "name": "csv"}),
    )
    return answer


def _read_alias_answer(request):
    answer = _read_answer(request)
    root = next(item for item in request.frontier.obligations if item.obligation_id == "request")
    assert root.end > root.start + 1
    alias = "inspect-csv-source"
    answer["scope"] = "subset"
    answer["obligation_ids"] = [alias]
    answer["obligations"] = [
        {
            "obligation_id": alias,
            "parent_id": "request",
            "description": "Inspect the CSV source contract before authoring.",
            "original_message_id": root.original_message_id,
            "start": root.start,
            "end": root.start + 1,
            "status": "open",
        }
    ]
    answer["remaining_actionable_ids"] = ["request", alias]
    answer["prescribed_action"]["obligation_ids"] = [alias]
    return answer


async def _recover_committed_read_owner(case, store, committed, count, owners):
    """Move this actual committed Read through original suspension and start."""
    _engine, repository, authority, _sessions, _sid = store
    recovered = dict(case)
    selected = committed
    for _ in range(count):
        previous = recovered["running"]
        original_root = decode_continuation_snapshot(selected.payload_json)
        previous_clock = recovered["sql_deadline"]
        previous_clock.begin_settlement()
        receipt = await recovered["lease"].consume_continuation_handoff(partial(repository.suspend_composer_segment, previous))
        repository.verify_recovery(receipt)
        await recovered["lease"].close()
        recovered["work"].coordinator.assert_completed()
        (claim,) = authority.claim_segment(limit=1)
        observation = ComposerSegmentStartObservation(claim)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local", observation=observation
        )
        running = ComposerOperationRunning(claim=claim, session_operation_context=context)
        assert observation.committed and observation.policy is not None and observation.hard_expires_at is not None
        clock = ContinuationSQLDeadlineOwner(
            running,
            policy=observation.policy,
            drain_signal=previous_clock.drain_signal,
            drain_seconds=previous_clock.drain_seconds,
            hard_expires_at=observation.hard_expires_at,
        )
        work = inventory_work(running, sql_deadline_owner=clock)
        lease = await ContinuationLeaseAdoption(repository, context, lease_seconds=30, required_work=work.coordinator).join()
        owners.callback(work.coordinator.assert_completed)
        owners.push_async_callback(lease.close)
        selected = authority.load_continuation_snapshot(running)
        assert decode_continuation_snapshot(selected.payload_json) == original_root.model_copy(update={"cursor": selected.cursor})
        assert running.claim.segment_sequence == previous.claim.segment_sequence
        assert running.claim.ownership_generation == previous.claim.ownership_generation + 1
        recovered.update(running=running, work=work, lease=lease, sql_deadline=clock)
    return recovered, selected


@pytest.mark.asyncio
@pytest.mark.parametrize("recoveries", (0, 1, 2))
async def test_actual_paid_read_installs_prepares_commits_original_and_reproves_cold(operation_store, tmp_path, monkeypatch, recoveries):
    async with _semantic_case(operation_store, tmp_path, monkeypatch, timed_read=True) as case, AsyncExitStack() as owners:
        physical = []
        sid = str(operation_store[4])
        with operation_store[0].connect() as conn:
            prior_attempts = len(conn.execute(select(attempts).where(attempts.c.session_id == sid)).all())
            prior_usage = len(conn.execute(select(usage).where(usage.c.session_id == sid)).all())

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply(json.dumps(_read_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        assert retained.decision is not None
        action = retained.decision.prescribed_action
        assert action is not None and action.kind == "read_tool"
        assert len(physical) == 1 and retained.audit_call.provider_cost == 0.01
        running = case["running"]
        semantic_key = prepared.binding.effect_key
        with locked_session_transaction(operation_store[0], sid) as conn:
            admission = read_semantic_action_admission_on_connection(conn, running, selected=retained.snapshot, effect_key=semantic_key)
            assert admission.action == action
            assert "get_plugin_schema" in admission.evidence.permitted_read_tools
            assert admission.evidence.coverage.complete is False
            with pytest.raises(AuditIntegrityError):
                read_semantic_action_admission_on_connection(
                    conn, running, selected=retained.snapshot, effect_key=semantic_key + ":foreign"
                )
            installed = install_semantic_action_on_connection(conn, running, selected=retained.snapshot, effect_key=semantic_key)
        with locked_session_transaction(operation_store[0], sid) as conn:
            installed_admission = require_installed_semantic_action_on_connection(
                conn, running, selected=installed, effect_key=semantic_key
            )
            assert installed_admission.action == action
            selected = prepare_semantic_read_on_connection(conn, running, selected=installed, effect_key=semantic_key)
        with locked_session_transaction(operation_store[0], sid) as conn:
            prepared_admission = require_semantic_read_execution_on_connection(conn, running, selected=selected, effect_key=semantic_key)
            assert prepared_admission.owner_effect_key is not None
            prepared_row = conn.execute(select(effects).where(effects.c.effect_key == prepared_admission.owner_effect_key)).one()
            assert prepared_row.owner == "read" and prepared_row.disposition == "prepared"
            assert prepared_row.receipt_key is prepared_row.result_chunk_key is None
        budget = case["provider"].cost_budget.snapshot()

        async def forbidden_sdk(**kwargs):
            raise AssertionError("a paid semantic read cannot replay Advisor or PRIMARY")

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        case["sql_deadline"].begin_action(case["sql_deadline"].policy.actions.read, started_at=time.monotonic())
        executed = await execute_semantic_advisor_read(
            service=case["runtime"].materialization.service,
            authority=case["runtime"].materialization.authority,
            running=running,
            selected=selected,
            effect_key=semantic_key,
            runtime=case["runtime"],
            required_work=case["work"],
        )
        assert executed.result.success and executed.committed.original_result.tool_name == "get_plugin_schema"
        assert executed.committed.original_result.prescribing_provider_call_id == retained.audit_call.call_id
        assert executed.committed.owner_effect_key == prepared_admission.owner_effect_key
        assert executed.committed.receipt.owner_receipt_id == prepared_admission.owner_effect_key
        assert executed.committed.receipt.disposition == "committed"
        with locked_session_transaction(operation_store[0], sid) as conn:
            row = conn.execute(select(effects).where(effects.c.effect_key == executed.committed.owner_effect_key)).one()
            assert row.disposition == "committed" and row.receipt_key == executed.committed.owner_effect_key
            assert row.result_chunk_key is not None
            retained_result = decode_read_owner_result(
                conn.execute(
                    select(chunks.c.payload).where(
                        chunks.c.session_id == sid,
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == row.result_chunk_key,
                    )
                )
                .scalar_one()
                .decode("utf-8")
            )
            assert retained_result == executed.committed.original_result
            cold = read_committed_semantic_read_on_connection(conn, running, selected=executed.committed.snapshot, effect_key=semantic_key)
            assert cold == executed.committed
            with pytest.raises(AuditIntegrityError):
                read_committed_semantic_read_on_connection(
                    conn, running, selected=executed.committed.snapshot, effect_key=semantic_key + ":foreign"
                )
            ledger_entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=retained.audit_call.call_id)
            savepoint = conn.begin_nested()
            try:
                changed = conn.execute(update(usage).where(usage.c.entry_id == ledger_entry_id).values(prompt_tokens=999))
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match="ledger"):
                    read_committed_semantic_read_on_connection(conn, running, selected=executed.committed.snapshot, effect_key=semantic_key)
            finally:
                savepoint.rollback()
            assert (
                read_committed_semantic_read_on_connection(conn, running, selected=executed.committed.snapshot, effect_key=semantic_key)
                == cold
            )
            savepoint = conn.begin_nested()
            try:
                changed = conn.execute(
                    update(effects)
                    .where(effects.c.effect_key == executed.committed.owner_effect_key)
                    .values(receipt_key="foreign-original-read")
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match="committed effect"):
                    read_committed_semantic_read_on_connection(conn, running, selected=executed.committed.snapshot, effect_key=semantic_key)
            finally:
                savepoint.rollback()
            assert (
                read_committed_semantic_read_on_connection(conn, running, selected=executed.committed.snapshot, effect_key=semantic_key)
                == cold
            )
        old_running = running
        case, consume_source = await _recover_committed_read_owner(case, operation_store, executed.committed.snapshot, recoveries, owners)
        running = case["running"]
        case["sql_deadline"].begin_action(case["sql_deadline"].policy.actions.session_sql, started_at=time.monotonic())
        with locked_session_transaction(operation_store[0], sid) as conn:
            before_consume = _rows_on_connection(conn)
            if recoveries:
                with pytest.raises(ComposerOperationFenceLost):
                    consume_committed_semantic_read_on_connection(conn, old_running, selected=consume_source, effect_key=semantic_key)
                assert _rows_on_connection(conn) == before_consume
        rollback = RuntimeError("roll back the actual completed read consumption")
        with pytest.raises(RuntimeError) as rolled_back, locked_session_transaction(operation_store[0], sid) as conn:
            provisional = consume_committed_semantic_read_on_connection(conn, running, selected=consume_source, effect_key=semantic_key)
            assert provisional.cursor == consume_source.cursor + 1
            assert _rows_on_connection(conn) != before_consume
            raise rollback
        assert rolled_back.value is rollback
        with locked_session_transaction(operation_store[0], sid) as conn:
            assert _rows_on_connection(conn) == before_consume
            assert load_snapshot(conn, session_id=sid, operation_id=running.claim.operation_id) == consume_source
            assert read_committed_semantic_read_on_connection(conn, running, selected=consume_source, effect_key=semantic_key) == cold
            consumed = consume_committed_semantic_read_on_connection(conn, running, selected=consume_source, effect_key=semantic_key)
        committed_root = decode_continuation_snapshot(executed.committed.snapshot.payload_json)
        consumed_root = decode_continuation_snapshot(consumed.payload_json)
        assert consumed_root.frames == committed_root.frames[:-1]
        (compose,) = consumed_root.frames
        assert type(compose) is ComposeFrame and compose.phase == "action_ready"
        assert consumed_root.active_action_json is consumed_root.active_action_source is None
        with locked_session_transaction(operation_store[0], sid) as conn:
            original = read_original_committed_semantic_read_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                commit_checkpoint_id=executed.committed.snapshot.checkpoint_id,
                semantic_effect_key=semantic_key,
            )
            assert original.committed == executed.committed
            assert original.admission.action == action
            assert original.admission.paid.audit_call == retained.audit_call
            inputs = read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=consumed)
            (read_input,) = inputs.reads
            assert read_input.owner_effect_key == executed.committed.owner_effect_key
            assert read_input.semantic_effect_key == semantic_key
            assert read_input.commit_checkpoint_id == executed.committed.snapshot.checkpoint_id
            assert read_input.commit_cursor == executed.committed.snapshot.cursor
            assert read_input.original_result_sha256 == stable_hash(executed.committed.original_result.model_dump(mode="json"))
            after_consume = _rows_on_connection(conn)
            with pytest.raises(AuditIntegrityError):
                consume_committed_semantic_read_on_connection(conn, running, selected=consumed, effect_key=semantic_key)
            assert _rows_on_connection(conn) == after_consume
            assert read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=consumed) == inputs
            bridge_key = f"semantic-read-source:{executed.committed.owner_effect_key}"
            bridge = json.loads(read_chunk(conn, session_id=sid, operation_id=running.claim.operation_id, key=bridge_key))
            assert bridge["commit_cursor"] == executed.committed.snapshot.cursor
            assert bridge["consume_cursor"] == consume_source.cursor + 1 == bridge["commit_cursor"] + recoveries + 1
            for field in ("commit_cursor", "consume_cursor", "original_result_sha256"):
                savepoint = conn.begin_nested()
                try:
                    changed_bridge = dict(bridge)
                    changed_bridge[field] = "0" * 64 if field == "original_result_sha256" else bridge[field] + 1
                    encoded = canonical_json(changed_bridge).encode()
                    changed = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == sid,
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == bridge_key,
                        )
                        .values(payload=encoded, byte_length=len(encoded), sha256=sha256(encoded).hexdigest())
                    )
                    assert changed.rowcount == 1
                    with pytest.raises(AuditIntegrityError):
                        read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=consumed)
                finally:
                    savepoint.rollback()
                assert _rows_on_connection(conn) == after_consume
            assert read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=consumed) == inputs
        with operation_store[0].connect() as conn:
            assert len(conn.execute(select(attempts).where(attempts.c.session_id == sid)).all()) == prior_attempts + 1
            assert len(conn.execute(select(usage).where(usage.c.session_id == sid)).all()) == prior_usage + 1
        assert len(physical) == 1 and case["provider"].cost_budget.snapshot() == budget
        if recoveries:
            # The full-app handover control owns the following fresh provider
            # stage; this SQL control does not reuse the retired provider owner.
            return
        # A fresh actual phase owner must restore the popped Compose cut;
        # no prior phase runtime or fabricated PRIMARY tool message is supplied.
        case["sql_deadline"].begin_action(case["sql_deadline"].policy.actions.advisor, started_at=time.monotonic())
        phases = ContinuationLivePhases(
            service=case["runtime"].materialization.service,
            authority=case["runtime"].materialization.authority,
            work=case["work"],
            provider_owner=case["provider"],
            lease=case["lease"],
            progress=case["runtime"].bindings.progress,
        )
        selected_after_pop = await load_selected_continuation(
            authority=phases.authority, running=running, selected=consumed, required_work=case["work"]
        )
        assert phases.runtime is None
        restored, _inventory = await phases.current(selected_after_pop)
        assert restored.source.selected is selected_after_pop
        assert restored.bindings.provider_owner is case["provider"]
        following = make_semantic_advisor_invocation(capture_semantic_advisor_source(restored))
        assert read_input.reference in following.frontier.evidence
        request = prepare_semantic_advisor_request(
            owner=phases.service._advisor_checkpoint,
            provider_owner=case["provider"],
            source=following.invocation,
            frontier=following.frontier,
            state=restored.live.state,
            content_scope=ComposerContentScope(UUID(sid), composition_content_hash(restored.live.state)),
            user_message=restored.bindings.message,
            runtime=restored,
            env_ref_names=restored.env_ref_names,
            retained_inputs=inputs,
        )
        document = request.prepared.evidence.messages[-1]
        assert document["role"] == "user"
        (prompt_result,) = json.loads(document["content"])["read_results"]
        assert canonical_json(prompt_result) == read_input.semantic_result_json
        assert prompt_result["tool_name"] == executed.committed.original_result.tool_name
        assert prompt_result["arguments"] == json.loads(executed.committed.original_result.arguments_json)
        assert prompt_result["response_json"] == executed.committed.original_result.response_json
        assert prompt_result["tool_result_json"] == executed.committed.original_result.tool_result_json
        assert prompt_result["blob_versions"] == [item.model_dump(mode="json") for item in executed.committed.original_result.blob_versions]
        assert prompt_result["attachment_inspections"] == [
            item.model_dump(mode="json") for item in executed.committed.original_result.attachment_inspections
        ]
        with operation_store[0].connect() as conn:
            assert len(conn.execute(select(attempts).where(attempts.c.session_id == sid)).all()) == prior_attempts + 1
            assert len(conn.execute(select(usage).where(usage.c.session_id == sid)).all()) == prior_usage + 1
        assert len(physical) == 1 and case["provider"].cost_budget.snapshot() == budget


@pytest.mark.asyncio
async def test_paid_read_canonicalizes_new_obligation_alias_through_effect_receipt_and_cold_reader(operation_store, tmp_path, monkeypatch):
    async with _semantic_case(operation_store, tmp_path, monkeypatch, timed_read=True) as case:
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply(json.dumps(_read_alias_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        assert retained.decision is not None
        raw_action = retained.decision.prescribed_action
        assert raw_action is not None and raw_action.kind == "read_tool"
        assert raw_action.obligation_ids == ("inspect-csv-source",)
        assert len(physical) == 1 and retained.audit_call.provider_cost == 0.01
        sid = str(operation_store[4])
        running = case["running"]
        semantic_key = prepared.binding.effect_key
        with locked_session_transaction(operation_store[0], sid) as conn:
            admitted = read_semantic_action_admission_on_connection(conn, running, selected=retained.snapshot, effect_key=semantic_key)
            assert admitted.action is not None and admitted.action.obligation_ids[0].startswith("obligation:")
            assert admitted.action.obligation_ids != raw_action.obligation_ids
            assert admitted.action_key is not None
            installed = install_semantic_action_on_connection(conn, running, selected=retained.snapshot, effect_key=semantic_key)
        with locked_session_transaction(operation_store[0], sid) as conn:
            installed_action = require_installed_semantic_action_on_connection(conn, running, selected=installed, effect_key=semantic_key)
            assert installed_action.action == admitted.action and installed_action.action_key == admitted.action_key
            selected = prepare_semantic_read_on_connection(conn, running, selected=installed, effect_key=semantic_key)
        with locked_session_transaction(operation_store[0], sid) as conn:
            prepared_action = require_semantic_read_execution_on_connection(conn, running, selected=selected, effect_key=semantic_key)
            assert prepared_action.action == admitted.action and prepared_action.action_key == admitted.action_key
            assert prepared_action.owner_effect_key is not None
            row = conn.execute(select(effects).where(effects.c.effect_key == prepared_action.owner_effect_key)).one()
            assert row.action_json == canonical_json(admitted.action.model_dump(mode="json"))
            assert row.disposition == "prepared" and row.receipt_key is None

        async def forbidden_sdk(**kwargs):
            raise AssertionError("alias read cannot replay its original paid Advisor")

        monkeypatch.setattr(litellm, "acompletion", forbidden_sdk)
        case["sql_deadline"].begin_action(case["sql_deadline"].policy.actions.read, started_at=time.monotonic())
        executed = await execute_semantic_advisor_read(
            service=case["runtime"].materialization.service,
            authority=case["runtime"].materialization.authority,
            running=running,
            selected=selected,
            effect_key=semantic_key,
            runtime=case["runtime"],
            required_work=case["work"],
        )
        assert executed.result.success
        assert executed.committed.receipt.action_key == admitted.action_key
        assert executed.committed.owner_effect_key == prepared_action.owner_effect_key
        assert executed.committed.original_result.prescribing_provider_call_id == retained.audit_call.call_id
        with locked_session_transaction(operation_store[0], sid) as conn:
            cold = read_committed_semantic_read_on_connection(conn, running, selected=executed.committed.snapshot, effect_key=semantic_key)
            assert cold == executed.committed
            row = conn.execute(select(effects).where(effects.c.effect_key == cold.owner_effect_key)).one()
            assert row.action_json == canonical_json(admitted.action.model_dump(mode="json"))
            assert row.disposition == "committed" and row.receipt_key == cold.owner_effect_key
        assert len(physical) == 1


@pytest.mark.asyncio
async def test_paid_whole_stop_refuses_incomplete_original_request_coverage(operation_store, tmp_path, monkeypatch):
    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            answer = _answer(case["request"])
            answer.update(
                decision="STOP_CONFIRMED",
                reason="The original request is complete.",
                remaining_actionable_ids=[],
                prescribed_action=None,
            )
            return _reply(json.dumps(answer))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        assert retained.decision is not None and retained.decision.decision == "STOP_CONFIRMED"
        sid = str(operation_store[4])
        with locked_session_transaction(operation_store[0], sid) as conn:
            paid = read_current_paid_semantic_decision_on_connection(
                conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
            assert paid.decision.prescribed_action is None
            assert derive_paid_semantic_evidence(paid).coverage.complete is False
            with pytest.raises(ContinuationProtocolError, match="open obligation"):
                install_semantic_action_on_connection(
                    conn, case["running"], selected=retained.snapshot, effect_key=prepared.binding.effect_key
                )
        assert len(physical) == 1 and retained.audit_call.provider_cost == 0.01
