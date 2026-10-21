"""Per-version identity must preserve exact interpretation-cohort replay."""

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.state_envelope import envelope_state_column
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import _prepared_pipeline

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["unrelated_metadata", "predecessor_publication"])
async def test_opt_out_replay_rejects_metadata_tampering_beside_fresh_publication(operation_store, tamper):
    engine, _repository, _authority, service, sid = operation_store
    row, arguments, drafts, _record, _running = await _prepared_pipeline(operation_store, with_review=True, opted_out=True)
    result = await service.settle_pipeline_composition_proposal(**arguments)
    restored = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    assert await service.replay_pipeline_composition_proposal(authority=restored, prepared_interpretations=drafts) == result
    metadata = deep_thaw(result.state.composer_meta)
    if tamper == "unrelated_metadata":
        metadata["foreign_evidence"] = "must not be laundered by rebinding"
    else:
        metadata["content_publication"] = deep_thaw(result.accepted_state.composer_meta)["content_publication"]
    with engine.begin() as connection:
        connection.execute(
            update(composition_states_table)
            .where(composition_states_table.c.id == str(result.state.id))
            .values(composer_meta=envelope_state_column(metadata))
        )
    with pytest.raises(AuditIntegrityError):
        await service.replay_pipeline_composition_proposal(authority=restored, prepared_interpretations=drafts)
