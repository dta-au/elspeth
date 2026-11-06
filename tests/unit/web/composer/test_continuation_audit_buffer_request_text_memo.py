"""One-stage immutable request-text reuse keeps recorder values and blocks fresh."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web import async_workers
from elspeth.web.composer import continuation_audit_buffer as audit_buffer
from elspeth.web.composer import continuation_codec as codec
from elspeth.web.composer.continuation_audit_buffer import (
    _deduplicate_request,
    _prepared_request_text,
    _RecorderBlocks,
    _transfer_prepared_request_text,
    decode_recorder_snapshot,
    encode_recorder_snapshot,
    recorder_snapshot_json,
)
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.continuation_retention_memo import (
    RecorderRequestTextKey,
    borrow_v4_preparation_memo_scope,
    retention_stage_memo_scope,
    v4_preparation_memo_scope,
)
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record
from tests.unit.web.test_required_sql_finish_once import publication_ticket


def _request(messages, tools):
    captured = capture_provider_context(messages=messages, tools=tools)
    assert captured.disposition == "retained"
    return captured


def _receipt(canonical: str) -> dict[str, object]:
    return {"disposition": "retained", "canonical": canonical, "reasons": []}


def test_repeated_request_warm_projection_matches_cold_and_rebuilds_distinct_calls():
    shared = {"role": "system", "content": "Shared instructions."}
    first = _request([shared, {"role": "user", "content": "First"}], [{"type": "function", "function": {"name": "sample"}}])
    second = _request([shared, {"role": "user", "content": "Second"}], None)
    base, _metadata = actual_record()
    snapshot = replace(
        _snapshot(0),
        llm_calls=(
            replace(base, call_id="00000000-0000-4000-8000-000000000001", private_request=first),
            replace(base, call_id="00000000-0000-4000-8000-000000000002", private_request=first),
            replace(base, call_id="00000000-0000-4000-8000-000000000003", private_request=second),
        ),
    )
    cold = recorder_snapshot_json(snapshot)
    with retention_stage_memo_scope():
        warm = recorder_snapshot_json(snapshot)
        restored = decode_recorder_snapshot(encode_recorder_snapshot(snapshot))
    assert warm == cold
    assert restored == snapshot
    assert restored.llm_calls[0] is not restored.llm_calls[1]
    assert restored.llm_calls[0].private_request == restored.llm_calls[1].private_request
    document = json.loads(warm)
    requests = [call["private_request"] for call in document["snapshot"]["llm_calls"]]
    assert requests[0]["messages"] == requests[1]["messages"]
    assert requests[0]["messages"][0] == requests[2]["messages"][0]
    assert requests[0]["tools"] is not None and requests[2]["tools"] is None


def test_outer_receipt_checks_and_changed_same_object_survive_warm_text():
    canonical = canonical_json(
        {"schema_version": "x", "capture_boundary": "x", "messages": [{"role": "user", "content": "first"}], "tools": None}
    )
    request = _receipt(canonical)
    with retention_stage_memo_scope():
        first = _deduplicate_request(request, _RecorderBlocks())
        assert first is not request
        withheld = dict(request, disposition="withheld")
        assert _deduplicate_request(withheld, _RecorderBlocks()) is withheld
        for changed in (dict(request, reasons=["changed"]), dict(request, extra=True), dict(request, canonical=4)):
            with pytest.raises(AuditIntegrityError, match="closed receipt shape"):
                _deduplicate_request(changed, _RecorderBlocks())
        request["canonical"] = canonical_json(
            {"schema_version": "x", "capture_boundary": "x", "messages": [{"role": "user", "content": "changed"}], "tools": None}
        )
        changed = _deduplicate_request(request, _RecorderBlocks())
        assert changed != first


@pytest.mark.parametrize(
    "canonical",
    (
        "not-json",
        canonical_json({"messages": []}),
        canonical_json({"schema_version": "x", "capture_boundary": "x", "messages": {}, "tools": None}),
        '{"schema_version": "x", "capture_boundary":"x","messages":[],"tools":null}',
    ),
)
def test_nonprepared_or_noncanonical_context_keeps_original_request(canonical):
    request = _receipt(canonical)
    with retention_stage_memo_scope() as memo:
        assert _deduplicate_request(request, _RecorderBlocks()) is request
        assert _deduplicate_request(request, _RecorderBlocks()) is request
        assert memo.cached_entry_count == 0


def test_warm_transfer_reparses_mutated_return_values_and_rejects_changed_digest():
    canonical = canonical_json(
        {"schema_version": "x", "capture_boundary": "x", "messages": [{"role": "user", "content": "original"}], "tools": None}
    )
    request = _receipt(canonical)
    with retention_stage_memo_scope():
        first_blocks = _RecorderBlocks()
        first = _deduplicate_request(request, first_blocks)
        first["messages"][0] = "0" * 64
        original_key = next(iter(first_blocks.document()))
        first_blocks.document()[original_key]["content"] = "mutated"
        second_blocks = _RecorderBlocks()
        second = _deduplicate_request(request, second_blocks)
        assert second["messages"][0] == original_key
        assert second_blocks.document()[original_key]["content"] == "original"
    proof = _prepared_request_text(RecorderRequestTextKey(canonical, 1_048_576))
    assert proof is not None
    damaged = replace(proof, blocks=((proof.blocks[0][0], '{"role":"user","content":"changed"}'),))
    with pytest.raises(AuditIntegrityError, match="content address"):
        _transfer_prepared_request_text(damaged, _RecorderBlocks())


def test_aggregate_oversize_skips_cache_without_changing_valid_projection(monkeypatch):
    canonical = canonical_json(
        {"schema_version": "x", "capture_boundary": "x", "messages": [{"role": "user", "content": "x" * 600_000}], "tools": None}
    )
    request = _receipt(canonical)
    cold = _deduplicate_request(request, _RecorderBlocks())
    produced = []

    def observe_request_text(key):
        produced.append(key)
        return _prepared_request_text(key)

    monkeypatch.setattr(audit_buffer, "_prepared_request_text", observe_request_text)
    with retention_stage_memo_scope():
        for _ in range(2):
            assert _deduplicate_request(request, _RecorderBlocks()) == cold
    # The complete request key plus proof still exceeds its original bound.
    # Independent bounded block-hash DATA may be retained by another family.
    assert produced == [RecorderRequestTextKey(canonical, 1_048_576)] * 2


def _clock():
    return ContinuationActionClock(ContinuationActionPolicy(preparation=10, guard=1, quota=1, physical=1, retention=1))


@pytest.mark.asyncio
async def test_v4_alternating_request_proofs_cross_children_keep_bytes_and_mutable_transfers_fresh(monkeypatch):
    first = _request([{"role": "user", "content": "First"}], [{"type": "function", "function": {"name": "sample"}}])
    second = _request([{"role": "user", "content": "Second"}], None)
    base, _metadata = actual_record()
    snapshot = replace(
        _snapshot(0),
        llm_calls=tuple(
            replace(base, call_id=f"00000000-0000-4000-8000-{index:012d}", private_request=request)
            for index, request in enumerate((first, second, first), 1)
        ),
    )
    cold = recorder_snapshot_json(snapshot)
    produced = []

    def producer(key):
        produced.append(key.canonical)
        return _prepared_request_text(key)

    monkeypatch.setattr(audit_buffer, "_prepared_request_text", producer)
    with v4_preparation_memo_scope(_clock()) as parent:

        def project():
            with borrow_v4_preparation_memo_scope():
                return recorder_snapshot_json(snapshot)

        for _ in range(3):
            result = await async_workers.run_required_sql_finish_once(publication_ticket(), project)
            assert isinstance(result, RequiredSQLReturned) and result.value == cold
        assert produced == [first.canonical, second.canonical] and len(parent._v4_recorder_requests) == 2
        with borrow_v4_preparation_memo_scope():
            blocks = _RecorderBlocks()
            transferred = _deduplicate_request(_receipt(first.canonical), blocks)
            key = transferred["messages"][0]
            transferred["messages"][0] = "changed"
            blocks.document()[key]["content"] = "mutated"
        with borrow_v4_preparation_memo_scope():
            following = _RecorderBlocks()
            assert _deduplicate_request(_receipt(first.canonical), following)["messages"][0] == key
            assert following.document()[key]["content"] == "First"
        assert produced == [first.canonical, second.canonical]
    assert parent.cached_entry_count == parent._v4_recorder_request_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ("parser", "renderer", "sha"))
@pytest.mark.parametrize("unsupported", (False, True), ids=("function-key", "unhashable-callable-bypass"))
async def test_v4_request_subordinate_replacements_execute_without_stale_projection(monkeypatch, dependency, unsupported):
    canonical = _request([{"role": "user", "content": "original"}], None).canonical
    request = _receipt(canonical)
    cold = _deduplicate_request(request, _RecorderBlocks())
    produced = []
    changed_calls = []

    def producer(key):
        produced.append(key.canonical)
        return _prepared_request_text(key)

    monkeypatch.setattr(audit_buffer, "_prepared_request_text", producer)
    with v4_preparation_memo_scope(_clock()):
        for _ in range(2):
            with borrow_v4_preparation_memo_scope():
                assert _deduplicate_request(request, _RecorderBlocks()) == cold
        assert len(produced) == 1
        if dependency == "parser":
            original = audit_buffer.parse_json_strict
        elif dependency == "renderer":
            original = audit_buffer.canonical_json
        else:
            original = audit_buffer.hashlib.sha256

        def replacement(value):
            changed_calls.append(True)
            return original(value)

        class UnhashableReplacement:
            __hash__ = None

            def __call__(self, value):
                return replacement(value)

        selected = UnhashableReplacement() if unsupported else replacement
        if dependency == "parser":
            monkeypatch.setattr(audit_buffer, "parse_json_strict", selected)
        elif dependency == "renderer":
            monkeypatch.setattr(audit_buffer, "canonical_json", selected)
        else:
            monkeypatch.setattr(audit_buffer.hashlib, "sha256", selected)
        for _ in range(2):
            with borrow_v4_preparation_memo_scope():
                assert _deduplicate_request(request, _RecorderBlocks()) == cold
        assert changed_calls and len(produced) == (3 if unsupported else 2)


@pytest.mark.asyncio
async def test_v4_request_shape_changes_fallback_outer_guards_oversize_and_producer_error(monkeypatch):
    canonical = _request([{"role": "user", "content": "original"}], None).canonical
    request = _receipt(canonical)
    produced = []

    def producer(key):
        produced.append(key.canonical)
        return _prepared_request_text(key)

    monkeypatch.setattr(audit_buffer, "_prepared_request_text", producer)
    with v4_preparation_memo_scope(_clock()) as parent:
        with borrow_v4_preparation_memo_scope():
            _deduplicate_request(request, _RecorderBlocks())
        with pytest.raises(AuditIntegrityError, match="closed receipt shape"):
            _deduplicate_request(dict(request, reasons=["changed"]), _RecorderBlocks())
        assert len(produced) == 1
        original_keys = audit_buffer._CONTEXT_KEYS
        for keys in (original_keys | {"extra"}, list(original_keys)):
            monkeypatch.setattr(audit_buffer, "_CONTEXT_KEYS", keys)
            for _ in range(2):
                with borrow_v4_preparation_memo_scope():
                    assert _deduplicate_request(request, _RecorderBlocks()) is request
        assert len(produced) == 5
        monkeypatch.setattr(audit_buffer, "_CONTEXT_KEYS", original_keys)
        malformed = _receipt("not-json")
        assert _deduplicate_request(malformed, _RecorderBlocks()) is malformed
        huge = _receipt(
            canonical_json({"schema_version": "x", "capture_boundary": "x", "messages": [{"content": "x" * 600_000}], "tools": None})
        )
        before = len(parent._v4_recorder_requests)
        for _ in range(2):
            with borrow_v4_preparation_memo_scope():
                assert _deduplicate_request(huge, _RecorderBlocks()) is not huge
        assert produced[-2:] == [huge["canonical"]] * 2 and len(parent._v4_recorder_requests) == before
        original_error = RuntimeError("original request projection error")

        def failure(_key):
            raise original_error

        monkeypatch.setattr(audit_buffer, "_prepared_request_text", failure)
        with pytest.raises(RuntimeError) as raised:
            _deduplicate_request(request, _RecorderBlocks())
        assert raised.value is original_error
    assert parent.cached_entry_count == 0


@pytest.mark.asyncio
async def test_complete_recorder_data_reuses_equal_chunks_across_children_and_admits_every_call(monkeypatch):
    original = audit_buffer._decode_recorder_data
    original_admit = audit_buffer._admit_recorder_data
    produced, admitted = [], []

    def produce(text):
        produced.append(text)
        return original(text)

    def admit(key, data):
        admitted.append((key.canonical, data))
        original_admit(key, data)

    monkeypatch.setattr(audit_buffer, "_decode_recorder_data", produce)
    monkeypatch.setattr(audit_buffer, "_admit_recorder_data", admit)
    first = encode_recorder_snapshot(_snapshot(1))
    changed = encode_recorder_snapshot(_snapshot(2))
    with v4_preparation_memo_scope(_clock()) as parent:
        restored = []
        for chunk in (first, codec.PrivateChunk(first.reference, (" " + first.canonical)[1:]), changed, first):
            with borrow_v4_preparation_memo_scope() as child:
                restored.append(decode_recorder_snapshot(chunk))
                assert not child._recorder_data
        assert produced == [first.canonical, changed.canonical]
        assert len(admitted) == 4 and restored[0] is restored[1] is restored[3]
        assert parent._recorder_data_bytes > 0 and len(parent._recorder_data) == 2

        # Complete reference binding still precedes the warmed DATA lookup.
        for reference in (
            first.reference.model_copy(update={"digest": "0" * 64}),
            first.reference.model_copy(update={"byte_length": first.reference.byte_length + 1}),
            first.reference.model_copy(update={"codec": "composer.evidence.v2"}),
        ):
            with pytest.raises(AuditIntegrityError):
                decode_recorder_snapshot(codec.PrivateChunk(reference, first.canonical))
        assert len(admitted) == 4 and len(produced) == 2

        def replacement(text):
            return produce(text)

        monkeypatch.setattr(audit_buffer, "_decode_recorder_data", replacement)
        assert decode_recorder_snapshot(first) == restored[0]
        assert len(produced) == 3 and len(admitted) == 5
    assert parent._recorder_data_bytes == 0 and not parent._recorder_data


@pytest.mark.asyncio
@pytest.mark.parametrize("current_refusal", ("wire_bound", "credential_traversal"))
async def test_warmed_recorder_data_runs_current_real_admission(monkeypatch, current_refusal):
    from elspeth.web.composer import bounded_json
    from elspeth.web.credential_guard import CredentialMaterialRefused

    chunk = encode_recorder_snapshot(_snapshot(1))
    with v4_preparation_memo_scope(_clock()) as parent:
        expected = decode_recorder_snapshot(chunk)
        retained = parent._recorder_data_bytes
        if current_refusal == "wire_bound":
            monkeypatch.setattr(codec, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", chunk.reference.byte_length - 1)
            error, message = AuditIntegrityError, "admission bound"
        else:
            monkeypatch.setattr(bounded_json, "JSON_MAX_TOTAL_UTF8_BYTES", 8)
            error, message = CredentialMaterialRefused, ""
        with pytest.raises(error, match=message):
            decode_recorder_snapshot(chunk)
        assert parent._recorder_data_bytes == retained
        assert next(iter(parent._recorder_data.values())).data.snapshot is expected


@pytest.mark.asyncio
async def test_recorder_miss_preserves_model_then_credential_then_roundtrip_refusal(monkeypatch):
    from elspeth.web.composer import bounded_json
    from elspeth.web.credential_guard import CredentialMaterialRefused

    snapshot = _snapshot(1)
    chunk = encode_recorder_snapshot(snapshot)
    document = json.loads(chunk.canonical)
    del document["snapshot"]["llm_calls"][0]["finish_reason"]
    normalized = codec.encode_private_chunk(codec=audit_buffer.RECORDER_CODEC, canonical=canonical_json(document))
    del document["snapshot"]["invocations"]
    malformed = codec.encode_private_chunk(codec=audit_buffer.RECORDER_CODEC, canonical=canonical_json(document))
    with v4_preparation_memo_scope(_clock()) as parent:
        with monkeypatch.context() as patch:
            patch.setattr(bounded_json, "JSON_MAX_TOTAL_UTF8_BYTES", 8)
            with pytest.raises(AuditIntegrityError, match="owned codec"):
                decode_recorder_snapshot(malformed)
            with pytest.raises(CredentialMaterialRefused):
                decode_recorder_snapshot(normalized)
            with pytest.raises(CredentialMaterialRefused):
                decode_recorder_snapshot(chunk)
            assert not parent._recorder_data and parent._recorder_data_bytes == 0
        with pytest.raises(AuditIntegrityError, match="omitted or normalized"):
            decode_recorder_snapshot(normalized)
        assert not parent._recorder_data
        assert decode_recorder_snapshot(chunk) == snapshot
        assert len(parent._recorder_data) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ("fresh", "raw"))
async def test_fresh_and_raw_recorder_data_share_only_data_and_keep_each_current_admission(monkeypatch, first):
    chunk = encode_recorder_snapshot(_snapshot(1))
    original_produce, original_admit = audit_buffer._decode_recorder_data, audit_buffer._admit_recorder_data
    produced, raw_admitted = [], []

    def produce(text):
        result = original_produce(text)
        produced.append(result)
        return result

    def admit(key, data):
        raw_admitted.append(data)
        original_admit(key, data)

    monkeypatch.setattr(audit_buffer, "_decode_recorder_data", produce)
    monkeypatch.setattr(audit_buffer, "_admit_recorder_data", admit)
    with v4_preparation_memo_scope(_clock()) as parent:
        if first == "fresh":
            fresh_chunk, fresh = audit_buffer._encode_and_decode_recorder_document(chunk.canonical)
            raw = decode_recorder_snapshot(chunk)
        else:
            raw = decode_recorder_snapshot(chunk)
            fresh_chunk, fresh = audit_buffer._encode_and_decode_recorder_document(chunk.canonical)
        assert fresh_chunk == chunk and fresh is raw
        assert len(produced) == 1 and raw_admitted == produced
        retained = parent._recorder_data_bytes
        assert retained > 0
        monkeypatch.setattr(codec, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", chunk.reference.byte_length - 1)
        with pytest.raises(AuditIntegrityError, match="admission bound"):
            decode_recorder_snapshot(chunk)
        assert raw_admitted == produced * 2
        with pytest.raises(AuditIntegrityError, match="admission bound"):
            audit_buffer._encode_and_decode_recorder_document(chunk.canonical)
        assert len(produced) == 1 and parent._recorder_data_bytes == retained
    assert not parent._recorder_data and parent._recorder_data_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("omitted_field", "projected_credential", "projected_bound"))
async def test_fresh_recorder_projection_mismatch_keeps_admission_before_field_loss(monkeypatch, change):
    from elspeth.web.composer.withheld_replies import WithheldReply
    from elspeth.web.credential_guard import CredentialMaterialRefused

    snapshot = _snapshot(1)
    canonical = recorder_snapshot_json(snapshot)
    if change == "omitted_field":
        document = json.loads(canonical)
        del document["snapshot"]["llm_calls"][0]["finish_reason"]
        canonical = canonical_json(document)
        error, message = AuditIntegrityError, "omitted or normalized"
    else:
        text = (
            "https://user:password@example.com"
            if change == "projected_credential"
            else "x" * codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        )
        changed = replace(snapshot, withheld_replies=(WithheldReply("planner_prose_unadmitted", text),))
        projected = recorder_snapshot_json(changed)

        def changed_projection(_decoded):
            return projected

        monkeypatch.setattr(audit_buffer, "recorder_snapshot_json", changed_projection)
        error, message = (CredentialMaterialRefused, "") if change == "projected_credential" else (AuditIntegrityError, "admission bound")
    original = audit_buffer._admit_recorder_data
    admitted = []

    def admit(key, data):
        admitted.append((key.canonical, data.canonical))
        original(key, data)

    monkeypatch.setattr(audit_buffer, "_admit_recorder_data", admit)
    with v4_preparation_memo_scope(_clock()) as parent:
        with pytest.raises(error, match=message):
            audit_buffer._encode_and_decode_recorder_document(canonical)
        assert len(admitted) == 1 and admitted[0][0] != admitted[0][1]
        assert not parent._recorder_data and parent._recorder_data_bytes == 0


@pytest.mark.asyncio
async def test_fresh_recorder_changed_text_is_admitted_before_decode_even_with_warm_data(monkeypatch):
    from elspeth.web.composer.withheld_replies import WithheldReply
    from elspeth.web.credential_guard import CredentialMaterialRefused

    snapshot = _snapshot(1)
    canonical = recorder_snapshot_json(snapshot)
    changed = replace(snapshot, withheld_replies=(WithheldReply("planner_prose_unadmitted", "https://user:password@example.com"),))
    document = json.loads(recorder_snapshot_json(changed))
    del document["snapshot"]["invocations"]
    changed_text = canonical_json(document)
    original = audit_buffer._decode_recorder_data
    produced = []

    def produce(text):
        produced.append(text)
        return original(text)

    monkeypatch.setattr(audit_buffer, "_decode_recorder_data", produce)
    with v4_preparation_memo_scope(_clock()) as parent:
        chunk, restored = audit_buffer._encode_and_decode_recorder_document(canonical)
        assert restored == snapshot and chunk.canonical == canonical
        with pytest.raises(CredentialMaterialRefused):
            audit_buffer._encode_and_decode_recorder_document(changed_text)
        assert produced == [canonical] and len(parent._recorder_data) == 1
        # Independent raw input retains model-before-projected-credential order.
        encoded = changed_text.encode("utf-8")
        reference = chunk.reference.model_copy(update={"digest": codec.sha256(encoded).hexdigest(), "byte_length": len(encoded)})
        with pytest.raises(AuditIntegrityError, match="owned codec"):
            decode_recorder_snapshot(codec.PrivateChunk(reference, changed_text))
        assert produced == [canonical, changed_text]


@pytest.mark.parametrize("change", ("document_shape", "unknown_field", "cursor"))
def test_fresh_recorder_keeps_complete_schema_and_cursor_refusals(change):
    document = json.loads(recorder_snapshot_json(_snapshot(1)))
    if change == "document_shape":
        document["unexpected"] = []
    elif change == "unknown_field":
        document["snapshot"]["unexpected"] = []
    else:
        document["snapshot"]["drained"]["llm_calls"] = 2
    canonical = canonical_json(document)
    chunk = codec.encode_private_chunk(codec=audit_buffer.RECORDER_CODEC, canonical=canonical)
    with pytest.raises(AuditIntegrityError) as raw:
        decode_recorder_snapshot(chunk)
    with pytest.raises(AuditIntegrityError) as fresh:
        audit_buffer._encode_and_decode_recorder_document(canonical)
    assert str(fresh.value) == str(raw.value)
