"""Original current checkpoint text precedes its independent grammar decision."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_retained_physical_audit_call
from elspeth.web.composer.continuation_provider_outcomes import measure_physical_audit_call_size
from tests.unit.web.composer.test_continuation_checkpoint_observation import (
    _actual_checkpoint_case,
    _custody_refusal,
    _large_audit,
    _observe,
    _observe_data,
)
from tests.unit.web.composer.test_continuation_checkpoint_observation import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_actual_malformed_checkpoint_text_has_complete_original_return_data(operation_store, tmp_path, monkeypatch, phase):
    raw = "A reachable provider returned text outside the checkpoint JSON grammar."
    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase=phase, raw_content=raw) as case:
        physical = case.physical
        assert physical.attempt is not None and physical.audit_call is not None and physical.message is not None
        assert physical.message.content == raw and case.parsed is None
        complete = _observe(case)
        assert type(complete) is CheckpointReturnedResponseObservation
        assert decode_continuation_response(complete.response).message == physical.message
        assert decode_retained_physical_audit_call(complete.response) == physical.audit_call
        require_original_v4_checkpoint_returned_response(
            complete, attempt_id=physical.attempt.attempt_id, request_sha256=physical.audit_call.messages_hash
        )

        # The separate size kernel grants no live response, retry or decision.
        # In particular a copied oversized audit cannot replace the original
        # complete malformed response at the actual V4 custody boundary.
        oversized = _large_audit(physical)
        assert oversized.audit_call is not None and measure_physical_audit_call_size(oversized.audit_call).exceeded
        with pytest.raises(AuditIntegrityError, match=_custody_refusal(case)):
            _observe(case, oversized)
        with pytest.raises(AuditIntegrityError, match="guidance or accounting"):
            _observe_data(case, replace(oversized, guidance="substituted"))
        with pytest.raises(AuditIntegrityError):
            _observe(case, replace(physical, message=replace(physical.message, content="substituted"), guidance="substituted"))
        assert type(_observe(case)) is CheckpointReturnedResponseObservation
