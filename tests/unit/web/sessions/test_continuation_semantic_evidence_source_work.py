"""Real paid semantic reads retain fresh SQL closure and lexical evidence work."""

import json

import litellm
import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_evidence_application_data as application_data
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.sessions.test_continuation_semantic_advisor import (
    _answer,
    _reply,
    _rows,
    _rows_on_connection,
    _run_paid_semantic,
    _semantic_case,
)
from tests.unit.web.sessions.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_paid_semantic_read_reuses_original_admission_but_reads_complete_closure_twice(operation_store, tmp_path, monkeypatch):
    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        sid, operation = str(operation_store[4]), case["running"].claim.operation_id
        root = decode_continuation_snapshot(retained.snapshot.payload_json)
        before = _rows(operation_store[0])
        with locked_session_transaction(operation_store[0], sid) as conn:
            expected = semantic_sql._paid_result_data(conn, sid, operation, retained.snapshot, root, prepared.binding.effect_key)
            source = expected[1].source
            actual_load = semantic_sql.load_private_chunks
            actual_decode = semantic_sql.decode_turn_evidence
            actual_document = semantic_sql._require_retained_input_document
            actual_admit = application_data.admit_turn_evidence
            loads, decodes, documents, extensions = [], [], [], []

            def load(*args, **kwargs):
                result = actual_load(*args, **kwargs)
                if encode_continuation_snapshot(kwargs["stack"]) == source.selected_snapshot_json:
                    loads.append(result)
                return result

            def decode(chunk, **kwargs):
                result = actual_decode(chunk, **kwargs)
                if chunk == source.evidence:
                    decodes.append(result)
                return result

            def document(*args, **kwargs):
                result = actual_document(*args, **kwargs)
                documents.append(result)
                return result

            def admit(*args, **kwargs):
                result = actual_admit(*args, **kwargs)
                extensions.append(result)
                return result

            monkeypatch.setattr(semantic_sql, "load_private_chunks", load)
            monkeypatch.setattr(semantic_sql, "decode_turn_evidence", decode)
            monkeypatch.setattr(semantic_sql, "_require_retained_input_document", document)
            monkeypatch.setattr(application_data, "admit_turn_evidence", admit)
            actual = semantic_sql._paid_result_data(conn, sid, operation, retained.snapshot, root, prepared.binding.effect_key)
            assert actual == expected and actual[0] == retained
            assert actual[0].snapshot.payload_json == retained.snapshot.payload_json
            assert encode_continuation_snapshot(actual[2]) == retained.snapshot.payload_json
            assert len(loads) == 2 and loads[0] == loads[1]
            assert len(decodes) == len(documents) == len(extensions) == 1
            assert documents[0].admitted is decodes[0]
            assert documents[0].source_snapshot_json == source.selected_snapshot_json
            assert extensions[0].source_chunk != source.evidence
            assert extensions[0].recorder_chunk == retained.recorder
            assert _rows_on_connection(conn) == before

            # Independent callers still decode, and projection-only DATA cannot
            # supply the actual graph owned by the earlier document admission.
            projection = semantic_sql._SemanticEvidenceDocument(
                source.evidence,
                loads[0],
                source.env_ref_names,
                canonical_json(decodes[0].snapshot.model_dump(mode="json", by_alias=True, exclude={"transcript"})),
            )
            for prior in (None, projection):
                original, admitted = semantic_sql._original_evidence_data(conn, sid, operation, actual[1], original_evidence=prior)
                assert encode_continuation_snapshot(original) == source.selected_snapshot_json
                assert admitted.source_chunk == source.evidence and admitted is not decodes[0]
            assert len(loads) == 4 and len(decodes) == 3 and len(extensions) == 1
            assert _rows_on_connection(conn) == before
        assert len(sdk) == 1 and _rows(operation_store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_ledger", (False, True))
async def test_paid_semantic_late_closure_corruption_keeps_fresh_read_and_physical_error_order(
    operation_store, tmp_path, monkeypatch, corrupt_ledger
):
    async with _semantic_case(operation_store, tmp_path, monkeypatch) as case:
        sdk = []

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(case["request"])))

        monkeypatch.setattr(litellm, "acompletion", completion)
        prepared, retained = await _run_paid_semantic(case, monkeypatch)
        sid, operation = str(operation_store[4]), case["running"].claim.operation_id
        root = decode_continuation_snapshot(retained.snapshot.payload_json)
        before = _rows(operation_store[0])
        actual_input = semantic_sql._original_input_data
        actual_load = semantic_sql.load_private_chunks
        loads, closures, mutations = [], [], []

        def load(*args, **kwargs):
            loads.append(kwargs["stack"])
            result = actual_load(*args, **kwargs)
            closures.append(result)
            return result

        def original_input(conn, *args):
            result = actual_input(conn, *args)
            data, original_evidence = result.data, result.original_evidence
            original = decode_continuation_snapshot(data.source.selected_snapshot_json)
            reference = original.frames[0].messages
            assert reference in {chunk.reference for chunk in closures[0]}
            assert reference not in {chunk.reference for chunk in original_evidence.admitted.dependency_chunks}
            changed = conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == sid,
                    chunks.c.operation_id == operation,
                    chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                )
                .values(sha256="0" * 64)
            )
            assert changed.rowcount == 1
            if corrupt_ledger:
                entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=retained.audit_call.call_id)
                changed = conn.execute(update(usage).where(usage.c.entry_id == entry_id).values(prompt_tokens=123456))
                assert changed.rowcount == 1
            mutations.append(reference)
            return result

        monkeypatch.setattr(semantic_sql, "load_private_chunks", load)
        monkeypatch.setattr(semantic_sql, "_original_input_data", original_input)
        message = "original complete ledger" if corrupt_ledger else "private chunk complete byte bound or SQL binding mismatch"
        with (
            pytest.raises(AuditIntegrityError, match=message),
            locked_session_transaction(operation_store[0], sid) as conn,
        ):
            semantic_sql._paid_result_data(conn, sid, operation, retained.snapshot, root, prepared.binding.effect_key)
        assert len(mutations) == 1 and len(loads) == (1 if corrupt_ledger else 2)
        assert len(sdk) == 1 and _rows(operation_store[0]) == before
