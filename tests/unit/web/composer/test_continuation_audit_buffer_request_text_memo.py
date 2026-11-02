"""One-stage immutable request-text reuse keeps recorder values and blocks fresh."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_audit_buffer import (
    _deduplicate_request,
    _prepared_request_text,
    _RecorderBlocks,
    _transfer_prepared_request_text,
    decode_recorder_snapshot,
    encode_recorder_snapshot,
    recorder_snapshot_json,
)
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.continuation_retention_memo import RecorderRequestTextKey, retention_stage_memo_scope
from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record


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


def test_aggregate_oversize_skips_cache_without_changing_valid_projection():
    canonical = canonical_json(
        {"schema_version": "x", "capture_boundary": "x", "messages": [{"role": "user", "content": "x" * 600_000}], "tools": None}
    )
    request = _receipt(canonical)
    cold = _deduplicate_request(request, _RecorderBlocks())
    with retention_stage_memo_scope() as memo:
        assert _deduplicate_request(request, _RecorderBlocks()) == cold
        assert memo.cached_entry_count == 0
