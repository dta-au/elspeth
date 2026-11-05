"""Original preview return/size DATA controls; live overflow is separate.

These controls use the actual canonical empty producer and native codec.
Prospective fallback documents grant no issue, SQL or terminal authority.
"""

from asyncio import CancelledError
from dataclasses import replace
from typing import get_args

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_value_batch_preview as preview
from elspeth.web.composer.continuation import EffectFrontierEntry, PrivateChunkRef
from elspeth.web.composer.continuation_capture_overflow import ContinuationMandatoryCaptureOverflow
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.sessions import continuation_preview_result as result_owner
from tests.unit.web.sessions import test_continuation_value_batch_preview as preview_fixture
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_value_batch_preview import prepared_preview

operation_store = preview_fixture.operation_store


def _native_document_excess():
    encoded = canonical_json({"original": "x" * CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES})
    with pytest.raises(AuditIntegrityError) as refused:
        encode_private_chunk(codec="composer.messages.v1", canonical=encoded)
    assert type(refused.value.__cause__) is ContinuationMandatoryCaptureOverflow
    assert refused.value.__cause__.witness.observed_bytes == len(encoded.encode("utf-8"))
    return refused.value


@pytest.mark.asyncio
async def test_preview_tail_observation_keeps_original_complete_return_and_native_error(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, sdk):
        original = await preview.observe_empty_preview_value(prepared)
        returned = result_owner.project_original_preview_return(prepared, source=original.source, result=original.result)
        assert type(returned) is result_owner.CompletePreviewReturn
        assert returned.chunk.reference.byte_length < CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        before = stored_rows(operation_store[0])
        error = _native_document_excess()
        observed = result_owner.observe_preview_tail_excess(returned, error)
        assert type(observed) is result_owner.PreviewTailExcess
        assert observed.original is error and observed.returned is returned
        result_owner.require_original_preview_tail_excess(observed, returned)
        receipt = observed.receipt
        assert receipt.reason == "complete_preview_tail_exceeds_limit" and receipt.surface == "private_chunk"
        assert receipt.limit == CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        assert receipt.observed_at_least == receipt.limit + 1 and receipt.measurement == "lower_bound"
        assert receipt.original_result is None and receipt.additional_provider_calls == 0 and receipt.additional_provider_cost_usd == "0"
        assert not receipt.missing_result_used and not receipt.automatic_retry_allowed
        # These are observation controls, not a fabricated live tail overflow.
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_preview_tail_observation_refuses_copy_mutation_and_unrelated_error(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, _):
        original = await preview.observe_empty_preview_value(prepared)
        returned = result_owner.project_original_preview_return(prepared, source=original.source, result=original.result)
        assert type(returned) is result_owner.CompletePreviewReturn
        error = _native_document_excess()
        observed = result_owner.observe_preview_tail_excess(returned, error)
        assert type(observed) is result_owner.PreviewTailExcess
        forged = result_owner.PreviewTailExcess(returned, error, observed.receipt)
        with pytest.raises(AuditIntegrityError, match="original source or measurement"):
            result_owner.require_original_preview_tail_excess(forged, returned)
        with pytest.raises(AuditIntegrityError, match="original source or measurement"):
            result_owner.require_original_preview_tail_excess(observed, replace(returned))
        observed.receipt = observed.receipt.model_copy(update={"observed_at_least": observed.receipt.limit + 2})
        with pytest.raises(AuditIntegrityError, match="original source or measurement"):
            result_owner.require_original_preview_tail_excess(observed, returned)
        for unrelated in (
            TimeoutError("retained limit"),
            AuditIntegrityError("credential/source refusal"),
            RuntimeError("capacity"),
            CancelledError("original Stop"),
            OperationalError("original publication", (), RuntimeError("original storage")),
        ):
            assert result_owner.observe_preview_tail_excess(returned, unrelated) is None
        observed = result_owner.observe_preview_tail_excess(returned, error)
        assert type(observed) is result_owner.PreviewTailExcess
        error.__cause__ = RuntimeError("foreign original cause")
        with pytest.raises(AuditIntegrityError, match="actual mandatory measurement"):
            result_owner.require_original_preview_tail_excess(observed, returned)


@pytest.mark.asyncio
async def test_preview_v1_and_v2_fallback_bounds_keep_closed_original_domains(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, _):
        original = await preview.observe_empty_preview_value(prepared)
        source = original.source
        root = prepared.runtime.source.selected.stack
        # Source-derived prospective capacity DATA only. No SQL head/phase,
        # source registry, issuer or effect row is installed by this value.
        planned = root.model_copy(
            update={
                "effect_frontier": (
                    *root.effect_frontier,
                    EffectFrontierEntry(
                        effect_id=f"tool:{source.response.digest}:{source.tool_index}", disposition="dispatched", receipt_id=None
                    ),
                )
            }
        )
        old = result_owner._fallback_documents(planned, source=source, version="composer.preview-hold.v1")
        new = result_owner._fallback_documents(planned, source=source, version="composer.preview-hold.v2")
        assert all(len(document.encode("utf-8")) <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES for document in (*old, *new))
        old_sizes = tuple(len(document.encode("utf-8")) for document in old)
        sizes = tuple(len(document.encode("utf-8")) for document in new)
        for surface in get_args(result_owner.PreviewByteSurface.__value__):
            receipt = result_owner.ValidationIncompleteReceipt(
                source=source, surface=surface, returned_sha256="a" * 64, returned_at="2026-11-05T00:00:00.000000+00:00"
            )
            assert receipt.schema_version == "composer.preview-incomplete.v1"
            docs = result_owner._receipt_documents(planned, receipt, "operation_failed")
            assert all(len(document.encode("utf-8")) <= bound for document, bound in zip(docs, old_sizes, strict=True))
        for surface in ("private_chunk", "evidence_pair", "retained_history"):
            limit = (
                prepared.runtime.source.policy.max_retained_payload_bytes
                if surface == "retained_history"
                else CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
            )
            for reference in (
                None,
                PrivateChunkRef(
                    codec="composer.preview-observation.v1", digest="a" * 64, byte_length=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
                ),
            ):
                receipt = result_owner.PreviewTailIncompleteReceipt(
                    source=source,
                    surface=surface,
                    limit=limit,
                    observed_at_least=limit + 1,
                    original_result=reference,
                    returned_sha256="a" * 64,
                    returned_at="2026-11-05T00:00:00.000000+00:00",
                )
                docs = result_owner._receipt_documents(planned, receipt, "request_cancelled")
                assert all(len(document.encode("utf-8")) <= bound for document, bound in zip(docs, sizes, strict=True))
                wire = canonical_json(receipt.model_dump(mode="json"))
                assert result_owner.PreviewTailIncompleteReceipt.model_validate_json(wire, strict=True) == receipt
                invalid = [
                    {"observed_at_least": limit},
                    {"additional_provider_calls": 1},
                    {"automatic_retry_allowed": True},
                    {"original_result": {"codec": "composer.messages.v1", "digest": "a" * 64, "byte_length": 1}},
                    {
                        "original_result": {
                            "codec": "composer.preview-observation.v1",
                            "digest": "a" * 64,
                            "byte_length": CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1,
                        }
                    },
                ]
                if surface != "retained_history":
                    invalid.append({"limit": limit + 1, "observed_at_least": limit + 2})
                for update in invalid:
                    with pytest.raises(ValidationError):
                        result_owner.PreviewTailIncompleteReceipt.model_validate_json(
                            canonical_json({**receipt.model_dump(mode="json"), **update}), strict=True
                        )
