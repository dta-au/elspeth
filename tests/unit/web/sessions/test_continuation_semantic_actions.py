"""Paid K-7 read/Stop actions through current SQL and the real read owner."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from uuid import UUID

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
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
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions import continuation_semantic_actions as action_sql
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import load_snapshot, read_chunk
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
    retain_semantic_advisor_outcome_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as attempts
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _reply
from tests.unit.web.sessions import test_continuation_semantic_publication as publication_fixture
from tests.unit.web.sessions.test_continuation_semantic_advisor import _begin, _invoke, _rows_on_connection, _semantic_case
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
        issuer, prepared, dispatched = _begin(store, case)
        observed = await _invoke(case, issuer)
        assert len(sdk) == 1 and observed.result.audit_call.provider_cost == 0.01
        with locked_session_transaction(store[0], str(store[4])) as conn:
            paid = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
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


@pytest.mark.asyncio
async def test_actual_paid_read_installs_prepares_commits_original_and_reproves_cold(operation_store, tmp_path, monkeypatch):
    async with _semantic_case(operation_store, tmp_path, monkeypatch, timed_read=True) as case:
        physical = []
        sid = str(operation_store[4])
        with operation_store[0].connect() as conn:
            prior_attempts = len(conn.execute(select(attempts).where(attempts.c.session_id == sid)).all())
            prior_usage = len(conn.execute(select(usage).where(usage.c.session_id == sid)).all())

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply(json.dumps(_read_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        assert observed.result.decision is not None
        action = observed.result.decision.prescribed_action
        assert action is not None and action.kind == "read_tool"
        assert len(physical) == 1 and observed.result.audit_call.provider_cost == 0.01
        running = case["running"]
        semantic_key = prepared.binding.effect_key
        with locked_session_transaction(operation_store[0], sid) as conn:
            retained = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
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
        assert executed.committed.original_result.prescribing_provider_call_id == observed.result.audit_call.call_id
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
            ledger_entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=observed.result.audit_call.call_id)
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
        case["sql_deadline"].begin_action(case["sql_deadline"].policy.actions.session_sql, started_at=time.monotonic())
        with locked_session_transaction(operation_store[0], sid) as conn:
            before_consume = _rows_on_connection(conn)
        rollback = RuntimeError("roll back the actual completed read consumption")
        with pytest.raises(RuntimeError) as rolled_back, locked_session_transaction(operation_store[0], sid) as conn:
            provisional = consume_committed_semantic_read_on_connection(
                conn, running, selected=executed.committed.snapshot, effect_key=semantic_key
            )
            assert provisional.cursor == executed.committed.snapshot.cursor + 1
            assert _rows_on_connection(conn) != before_consume
            raise rollback
        assert rolled_back.value is rollback
        with locked_session_transaction(operation_store[0], sid) as conn:
            assert _rows_on_connection(conn) == before_consume
            assert load_snapshot(conn, session_id=sid, operation_id=running.claim.operation_id) == executed.committed.snapshot
            assert (
                read_committed_semantic_read_on_connection(conn, running, selected=executed.committed.snapshot, effect_key=semantic_key)
                == cold
            )
            consumed = consume_committed_semantic_read_on_connection(
                conn, running, selected=executed.committed.snapshot, effect_key=semantic_key
            )
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
            assert original.admission.paid.audit_call == observed.result.audit_call
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
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        assert observed.result.decision is not None
        raw_action = observed.result.decision.prescribed_action
        assert raw_action is not None and raw_action.kind == "read_tool"
        assert raw_action.obligation_ids == ("inspect-csv-source",)
        assert len(physical) == 1 and observed.result.audit_call.provider_cost == 0.01
        sid = str(operation_store[4])
        running = case["running"]
        semantic_key = prepared.binding.effect_key
        with locked_session_transaction(operation_store[0], sid) as conn:
            retained = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
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
        assert executed.committed.original_result.prescribing_provider_call_id == observed.result.audit_call.call_id
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
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        assert observed.result.decision is not None and observed.result.decision.decision == "STOP_CONFIRMED"
        sid = str(operation_store[4])
        with locked_session_transaction(operation_store[0], sid) as conn:
            retained = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
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
        assert len(physical) == 1 and observed.result.audit_call.provider_cost == 0.01
