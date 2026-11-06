"""Checkpoint DATA controls borrowing a real current first-return custody."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_codec import decode_continuation_response
from tests.unit.web.composer.test_continuation_checkpoint_observation import (
    _actual_checkpoint_case,
    _observe,
    _observe_data,
)
from tests.unit.web.composer.test_continuation_checkpoint_observation import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_checkpoint_original_100k_metadata_matches_diagnostic_only_after_original_witness(operation_store, tmp_path, monkeypatch):
    original_reasoning = "r" * 100_000

    def sdk_response(response):
        response.choices[0].message.reasoning_content = original_reasoning
        return response

    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase="end", response_transform=sdk_response) as case:
        physical = case.physical
        assert physical.attempt is case.custody.attempt and physical.audit_call is case.custody.call
        assert physical.provider_metadata is not None and physical.message is not None and physical.audit_call is not None
        metadata = physical.provider_metadata
        witness = metadata.v4_completeness
        assert witness is not None

        result = _observe(case)
        assert type(result) is CheckpointReturnedResponseObservation
        assert result.provider_metadata is metadata and result.audit_call is physical.audit_call
        assert decode_continuation_response(result.response).provider_metadata.reasoning_content == original_reasoning
        assert result.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE

        with pytest.raises(AuditIntegrityError):
            _observe(case, replace(physical, provider_metadata=replace(metadata, reasoning_content="s" * 100_000)))
        assert _observe(case).response == result.response

        # Keep the original issued witness valid while testing the pure audit
        # comparator; a replaced call cannot borrow the live custody.
        witness.require_result(metadata, content=physical.message.content, tool_calls=())
        with pytest.raises(AuditIntegrityError, match="physical identity"):
            _observe_data(case, replace(physical, audit_call=replace(physical.audit_call, reasoning_content="changed audit")))
        assert _observe(case).response == result.response
