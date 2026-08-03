"""Private owned-state authority contracts for lossless freeform proposals."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer.audit import begin_dispatch, finish_success
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.authority_hashing import composer_authority_hash, project_composer_authority_payload
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_proposal import (
    OWNED_COMPOSITION_STATE_AUTHORITY,
    AbsentBase,
    PipelineProposal,
    PlannerSurface,
    is_owned_composition_state_authority,
    owned_composition_state_authority,
    owned_composition_state_execution_arguments,
    owned_composition_state_review_arguments,
    restore_owned_composition_state_authority,
)
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState
from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY
from elspeth.web.sessions.proposal_blob_refs import proposal_blob_reference_ids

_BLOB_A = "00000000-0000-4000-8000-00000000000a"
_BLOB_B = "00000000-0000-4000-8000-00000000000b"


def _state(*, version: int = 7, blob_ids: tuple[str, ...] = (_BLOB_A,)) -> CompositionState:
    sources = {
        f"source_{index}": {
            "plugin": "csv",
            "on_success": f"rows_{index}",
            "options": {
                "path": f"/private/blobs/session/{blob_id}_manifest.csv",
                "blob_ref": blob_id,
                "mode": "bind_source",
                SOURCE_AUTHORING_KEY: {"modality": "upload"},
                "schema": {"mode": "fixed", "fields": ["amount: float"]},
            },
            "on_validation_failure": "discard",
        }
        for index, blob_id in enumerate(blob_ids, start=1)
    }
    return CompositionState.from_dict(
        {
            "version": version,
            "sources": sources,
            "nodes": [
                {
                    "id": "prompt_shield_auto_1",
                    "node_type": "transform",
                    "plugin": "aws_bedrock_prompt_shield",
                    "input": "rows_1",
                    "on_success": "screened_rows",
                    "on_error": "discard",
                    "options": {
                        "profile": "deployment",
                        "interpretation_requirements": [
                            {
                                "id": "required_control_auto_wiring:prompt_shield_auto_1",
                                "kind": "pipeline_decision",
                                "user_term": "Required deployment control auto-wiring",
                                "status": "pending",
                                "draft": "The deployment policy requires this control.",
                                "event_id": None,
                                "accepted_value": None,
                                "accepted_artifact_hash": None,
                                "resolved_prompt_template_hash": None,
                            }
                        ],
                    },
                }
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "screened_rows",
                    "plugin": "json",
                    "options": {
                        "path": "/private/outputs/session/result.jsonl",
                        "schema": {"mode": "observed"},
                        "format": "jsonl",
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
            "metadata": {"name": "Owned proposal", "description": "Exact authored content"},
        }
    )


def _proposal(authority: dict[str, Any]) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline=authority,
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.FREEFORM,
        repair_count=0,
        skill_hash=stable_hash("composer-skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def _dispatch_result() -> dict[str, object]:
    return {
        "success": True,
        "validation": {
            "is_valid": True,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": ["prompt_shield_auto_1", "screened_rows"],
        "version": 8,
        "pipeline_content_hash_schema": "composer.pipeline-dispatch-result.v1",
        "pipeline_content_hash": stable_hash({"state": "owned"}),
    }


def test_owned_state_authority_round_trips_exact_content_without_lifecycle_version() -> None:
    state = _state()
    authority = owned_composition_state_authority(state)

    assert is_owned_composition_state_authority(authority)
    assert authority["authority_kind"] == OWNED_COMPOSITION_STATE_AUTHORITY
    assert "version" not in authority
    restored = restore_owned_composition_state_authority(authority, version=19)
    expected = state.to_dict()
    expected["version"] = 19
    assert restored.to_dict() == expected

    execution = owned_composition_state_execution_arguments(authority, version=19)
    assert execution["sources"] == expected["sources"]
    assert execution["outputs"][0] == {
        "sink_name": "screened_rows",
        "plugin": "json",
        "options": expected["outputs"][0]["options"],
        "on_write_failure": "discard",
    }

    proposal = _proposal(authority)
    assert PipelineProposal.from_dict(proposal.to_dict(), reviewed_facts={}) == proposal


def test_owned_state_review_projection_withholds_blob_custody_but_keeps_control_evidence() -> None:
    authority = owned_composition_state_authority(_state(blob_ids=(_BLOB_A, _BLOB_B)))
    review = owned_composition_state_review_arguments(authority)
    review_text = canonical_json(review)

    assert _BLOB_A not in review_text
    assert _BLOB_B not in review_text
    assert "/private/blobs/" not in review_text
    assert SOURCE_AUTHORING_KEY not in review_text
    assert review["nodes"][0]["plugin"] == "aws_bedrock_prompt_shield"
    assert review["sources"]["source_1"]["options"] == {"schema": {"mode": "fixed", "fields": ["amount: float"]}}
    assert proposal_blob_reference_ids("set_pipeline", authority) == (_BLOB_A, _BLOB_B)


@pytest.mark.parametrize("mutation", ["extra", "missing", "wrong_kind", "bad_version"])
def test_owned_state_authority_rejects_malformed_closed_shapes(mutation: str) -> None:
    authority = owned_composition_state_authority(_state())
    malformed = deepcopy(authority)
    version: object = 1
    if mutation == "extra":
        malformed["version"] = 7
    elif mutation == "missing":
        del malformed["sources"]
    elif mutation == "wrong_kind":
        malformed["authority_kind"] = "owned_composition_state.v2"
    elif mutation == "bad_version":
        version = True

    with pytest.raises(AuditIntegrityError, match="owned composition-state"):
        restore_owned_composition_state_authority(malformed, version=version)  # type: ignore[arg-type]


def test_owned_state_draft_hash_binds_every_private_source_identity() -> None:
    first = _proposal(owned_composition_state_authority(_state(blob_ids=(_BLOB_A,))))
    second = _proposal(owned_composition_state_authority(_state(blob_ids=(_BLOB_B,))))

    assert first.draft_hash != second.draft_hash


def test_owned_state_dispatch_keeps_private_binding_and_persists_only_review_projection() -> None:
    authority = owned_composition_state_authority(_state())
    audit = begin_dispatch("call-owned-state", "set_pipeline", authority, version_before=7, actor="test")
    invocation = finish_success(audit, result_payload=_dispatch_result(), version_after=8)

    private_binding = PipelineDispatchAuditBinding.from_invocation(invocation)
    assert private_binding.arguments_hash == composer_authority_hash(authority)
    assert _BLOB_A in invocation.arguments_canonical
    assert "/private/blobs/" in invocation.arguments_canonical

    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    persisted_text = json.dumps(persisted, sort_keys=True)
    assert _BLOB_A not in persisted_text
    assert "/private/blobs/" not in persisted_text
    assert "aws_bedrock_prompt_shield" in persisted_text

    expected_redacted = redact_tool_call_arguments(
        "set_pipeline",
        owned_composition_state_review_arguments(authority),
        telemetry=NoopRedactionTelemetry(),
    )
    expected_canonical = canonical_json(expected_redacted)
    expected_authority_canonical = canonical_json(project_composer_authority_payload(expected_redacted))
    assert persisted["arguments_canonical"] == expected_canonical
    assert persisted["arguments_hash"] == hashlib.sha256(expected_canonical.encode()).hexdigest()
    assert persisted["authority_arguments_canonical"] == expected_authority_canonical
    assert persisted["authority_arguments_hash"] == hashlib.sha256(expected_authority_canonical.encode()).hexdigest()

    persisted_binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)
    assert persisted_binding.arguments_hash == composer_authority_hash(expected_redacted)
    assert deep_thaw(authority)["sources"]["source_1"]["options"]["blob_ref"] == _BLOB_A
