"""The Composer core exposes one authoring and proposal contract."""

from dataclasses import fields

from elspeth.web.composer.pipeline_proposal import PipelineProposal, PipelineProposalData
from elspeth.web.composer.state import CompositionState


def test_composition_state_has_no_retired_guided_checkpoint() -> None:
    assert "guided_session" not in {item.name for item in fields(CompositionState)}


def test_proposal_has_no_mode_discriminator() -> None:
    assert "surface" not in {item.name for item in fields(PipelineProposal)}


def test_proposal_wire_has_only_live_freeform_authority() -> None:
    retired = {"surface", "reviewed_anchor_hash", "covered_deferred_intent_ids", "supersedes_draft_hash"}
    assert not retired & PipelineProposalData.__annotations__.keys()
