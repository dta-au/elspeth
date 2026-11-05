"""Actual semantic selector and same-transaction document DATA boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import app as app_module
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_events_table as events
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, install_sdk, submit_and_settle, tool_call
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture
from tests.unit.web.sessions.test_continuation_semantic_advisor import _issuer, _prepare, _rows, _rows_on_connection, _semantic_case

operation_store = semantic_fixture.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end_candidate,fault", ((False, None), (True, None), (False, "inventory"), (False, "state_bytes"), (False, "frontier_bytes"))
)
async def test_unchanged_semantic_selector_keeps_actual_local_proof_without_history_replay(
    operation_store, tmp_path, monkeypatch, end_candidate, fault
):
    async with _semantic_case(operation_store, tmp_path, monkeypatch, end_candidate=end_candidate) as case:
        runtime = case["runtime"]
        selected, inventory = runtime.source.selected, runtime.materialization.inventory
        full_reads = []
        original = semantic_sql.read_runtime_source_on_connection

        def read(*args, **kwargs):
            full_reads.append(args[0])
            return original(*args, **kwargs)

        monkeypatch.setattr(semantic_sql, "read_runtime_source_on_connection", read)
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            transaction = conn.begin_nested()
            try:
                if fault == "inventory":
                    inventory = replace(inventory, selection=replace(inventory.selection, cursor=inventory.selection.cursor + 1))
                elif fault in ("state_bytes", "frontier_bytes"):
                    reference = selected.stack.frames[0].state if fault == "state_bytes" else selected.stack.semantic_frontier
                    assert reference is not None
                    changed = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(operation_store[4]),
                            chunks.c.operation_id == case["running"].claim.operation_id,
                            chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                        )
                        .values(sha256="0" * 64)
                    )
                    assert changed.rowcount == 1
                if fault is None:
                    result = semantic_sql.select_semantic_advisor_current_graph_on_connection(
                        conn, case["running"], selected=selected, inventory=inventory
                    )
                    assert result.checkpoint_id == selected.checkpoint_id and result.cursor == selected.stack.cursor
                    assert _rows_on_connection(conn) == before
                else:
                    with pytest.raises(AuditIntegrityError):
                        semantic_sql.select_semantic_advisor_current_graph_on_connection(
                            conn, case["running"], selected=selected, inventory=inventory
                        )
                assert full_reads == []
            finally:
                transaction.rollback()
        assert _rows(operation_store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handoff", ("equal", "cold", "other_source", "other_dependencies", "other_names", "bad_projection", "noncanonical")
)
async def test_semantic_document_reuses_only_actual_equal_data_and_keeps_original_fallback(operation_store, tmp_path, monkeypatch, handoff):
    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        full_reads, document_decodes, observed = [], [], []
        original_read = semantic_sql.read_runtime_source_on_connection
        original_decode = semantic_sql.decode_turn_evidence
        original_document = semantic_sql._require_retained_input_document

        def read(*args, **kwargs):
            result = original_read(*args, **kwargs)
            full_reads.append(result)
            return result

        def decode(*args, **kwargs):
            document_decodes.append(args[0])
            return original_decode(*args, **kwargs)

        def document(*args, **kwargs):
            value = kwargs["evidence_document"]
            assert type(value) is semantic_sql._SemanticEvidenceDocument
            assert value.source == full_reads[-1].admitted_evidence.source_chunk
            assert value.source_chunks == full_reads[-1].selected.chunks
            observed.append(value)
            if handoff == "cold":
                kwargs.pop("evidence_document")
            elif handoff == "other_source":
                other = next(chunk for chunk in value.source_chunks if chunk.reference != value.source.reference)
                kwargs["evidence_document"] = replace(value, source=other)
            elif handoff == "other_dependencies":
                kwargs["evidence_document"] = replace(value, source_chunks=(*value.source_chunks, value.source))
            elif handoff == "other_names":
                kwargs["evidence_document"] = replace(value, env_ref_names=value.env_ref_names | {"EXTRA_PROJECTION_CONTROL_NAME"})
            elif handoff == "bad_projection":
                kwargs["evidence_document"] = replace(value, projection_json="{}")
            elif handoff == "noncanonical":
                kwargs["evidence_document"] = replace(value, projection_json=value.projection_json + " ")
            return original_document(*args, **kwargs)

        monkeypatch.setattr(semantic_sql, "read_runtime_source_on_connection", read)
        monkeypatch.setattr(semantic_sql, "decode_turn_evidence", decode)
        monkeypatch.setattr(semantic_sql, "_require_retained_input_document", document)
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            transaction = conn.begin_nested()
            try:
                if handoff in ("bad_projection", "noncanonical"):
                    message = "complete original source/result DATA" if handoff == "bad_projection" else "canonical source projection"
                    with pytest.raises(AuditIntegrityError, match=message):
                        _prepare(conn, case, _issuer(case))
                    assert _rows_on_connection(conn) == before
                else:
                    prepared = _prepare(conn, case, _issuer(case))
                    assert prepared.binding.input_sha256 == case["request"].request_sha256
                assert len(full_reads) == len(observed) == 1
                assert len(document_decodes) == (0 if handoff in ("equal", "bad_projection", "noncanonical") else 1)
            finally:
                transaction.rollback()
        assert _rows(operation_store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("evidence", "dependency"))
async def test_semantic_document_rereads_actual_bytes_after_fresh_runtime_proof(operation_store, tmp_path, monkeypatch, role):
    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        original = semantic_sql._require_retained_input_document
        reached = []

        def document(conn, *args, **kwargs):
            value = kwargs["evidence_document"]
            reference = (
                value.source.reference
                if role == "evidence"
                else next(chunk.reference for chunk in value.source_chunks if chunk.reference != value.source.reference)
            )
            changed = conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(operation_store[4]),
                    chunks.c.operation_id == case["running"].claim.operation_id,
                    chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                )
                .values(sha256="0" * 64)
            )
            assert changed.rowcount == 1
            reached.append(reference)
            return original(conn, *args, **kwargs)

        monkeypatch.setattr(semantic_sql, "_require_retained_input_document", document)
        before = _rows(operation_store[0])
        with (
            pytest.raises(AuditIntegrityError),
            locked_session_transaction(operation_store[0], str(operation_store[4])) as conn,
        ):
            _prepare(conn, case, _issuer(case))
        assert len(reached) == 1
        assert _rows(operation_store[0]) == before


@pytest.mark.asyncio
async def test_real_mutation_selector_reproves_history_before_frontier_append_and_rolls_it_back(tmp_path, monkeypatch):
    from elspeth.web.composer import continuation_semantic_advisor_phase as phase
    from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline

    class SelectionRollbackProbe(Exception):
        pass

    def policy(settings):
        actual = continuation_policy_v4_from_settings(settings)
        assert actual is not None and actual.actions.advisor.preparation == 5.0
        return actual

    monkeypatch.setattr(app_module, "continuation_policy_from_settings", policy)
    operation = str(uuid4())
    reached, full_reads = [], []
    original_select = phase.select_semantic_advisor_current_graph_on_connection
    original_read = semantic_sql.read_runtime_source_on_connection
    inside_selector = False

    def graph_events(conn):
        payloads = conn.execute(
            select(chunks.c.payload)
            .select_from(
                events.join(
                    chunks,
                    (events.c.session_id == chunks.c.session_id)
                    & (events.c.operation_id == chunks.c.operation_id)
                    & (events.c.payload_chunk_key == chunks.c.chunk_key),
                )
            )
            .where(events.c.operation_id == operation)
        ).scalars()
        return tuple(body for payload in payloads if (body := json.loads(payload)).get("kind") == "semantic_current_graph")

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if inside_selector:
            full_reads.append(result)
        return result

    def selector(conn, running, **kwargs):
        nonlocal inside_selector
        if running.claim.operation_id != operation:
            return original_select(conn, running, **kwargs)
        before = _rows_on_connection(conn)
        inside_selector = True
        try:
            result = original_select(conn, running, **kwargs)
        finally:
            inside_selector = False
        selected = kwargs["selected"]
        assert result.checkpoint_id != selected.checkpoint_id and result.cursor == selected.stack.cursor + 1
        assert len(full_reads) == 1 and _rows_on_connection(conn) != before
        assert len(graph_events(conn)) == 1
        reached.append(result)
        raise SelectionRollbackProbe("controlled rollback after actual graph selection")

    monkeypatch.setattr(semantic_sql, "read_runtime_source_on_connection", read)
    monkeypatch.setattr(phase, "select_semantic_advisor_current_graph_on_connection", selector)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        blob = tmp_path / "blobs" / str(harness.session_id)
        blob.mkdir(parents=True, exist_ok=True)
        (blob / "input.csv").write_text("name\nAda\n", encoding="utf-8")
        (tmp_path / "outputs" / str(harness.session_id)).mkdir(parents=True, exist_ok=True)
        await harness.app.state.session_service.update_composer_preferences(
            harness.session_id, trust_mode="auto_commit", density_default="high", actor="test"
        )

        def primary(app):
            return "Building the requested draft.", [
                tool_call(app, "projection_author", "set_pipeline", _pipeline(tmp_path, str(harness.session_id)))
            ]

        clean = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The provider's draft is coherent.", "note": None}
        sdk = ScriptedSDK([("primary", primary), ("early", clean)], app_ref=[harness.app])
        install_sdk(monkeypatch, sdk)
        terminal = await submit_and_settle(harness, message_body("Build and explain the requested pipeline.", operation_id=operation))
        assert len(reached) == 1, "The original mutation driver must reach actual changed graph selection before fault injection"
        assert terminal.status == "failed" and sdk.roles == ["primary", "early"] and not sdk.steps
        with harness.app.state.session_engine.connect() as conn:
            assert graph_events(conn) == ()
