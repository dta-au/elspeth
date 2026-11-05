"""One-stage immutable request-text reuse keeps recorder values and blocks fresh."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web import async_workers
from elspeth.web.composer import continuation_audit_buffer as audit_buffer
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
