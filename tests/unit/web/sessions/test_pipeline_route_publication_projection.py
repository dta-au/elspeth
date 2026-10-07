"""Actual review-derived SQL heads retain candidate and route projection identity."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.routes.composer.pipeline_settlement import _validate_pipeline_publication_result
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import _prepared_pipeline

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
async def test_route_projection_accepts_actual_distinct_review_head_and_refuses_corrupted_candidate(operation_store):
    _engine, repository, _authority, service, sid = operation_store
    row, arguments, _drafts, _record, running = await _prepared_pipeline(operation_store, with_review=True, opted_out=True)
    try:
        result = await service.settle_pipeline_composition_proposal(**arguments)
        assert result.accepted_state is not None
        assert result.state.session_id == sid == result.accepted_state.session_id
        assert result.accepted_state.id != result.state.id
        assert result.state.derived_from_state_id == result.accepted_state.id
        assert result.proposal.committed_state_id == result.accepted_state.id
        assert result.transition_message is not None
        assert result.transition_message.composition_state_id == result.state.id
        _validate_pipeline_publication_result(result, row)
        corrupted = replace(result, proposal=replace(result.proposal, committed_state_id=result.state.id))
        with pytest.raises(AuditIntegrityError, match="inconsistent committed evidence"):
            _validate_pipeline_publication_result(corrupted, row)
    finally:
        repository.release(running.session_operation_context)
