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


def _authority_payload() -> dict[str, Any]:
    """Return a mutable owned-state authority payload for staleness probes."""
    return deepcopy(owned_composition_state_authority(_state()))


def _coalesce_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "join_rows",
        "node_type": "coalesce",
        "plugin": None,
        "input": "screened_rows",
        "on_success": "joined_rows",
        "on_error": None,
        "options": {},
        "branches": ["left", "right"],
    }
    node.update(overrides)
    return node


@pytest.mark.parametrize(
    ("section", "mutate"),
    [
        ("node", lambda payload: payload["nodes"][0].update({"undeclared": "x"})),
        ("source", lambda payload: payload["sources"]["source_1"].update({"undeclared": "x"})),
        ("output", lambda payload: payload["outputs"][0].update({"undeclared": "x"})),
        (
            "edge",
            lambda payload: payload["edges"].append(
                {
                    "id": "e1",
                    "from_node": "source",
                    "to_node": "prompt_shield_auto_1",
                    "edge_type": "on_success",
                    "label": None,
                    "undeclared": "x",
                }
            ),
        ),
        ("metadata", lambda payload: payload["metadata"].update({"undeclared": "x"})),
    ],
)
def test_owned_state_authority_names_a_field_the_state_model_does_not_define(
    section: str,
    mutate: Any,
) -> None:
    payload = _authority_payload()
    mutate(payload)

    with pytest.raises(AuditIntegrityError, match="carries a field the composer state model does not define"):
        restore_owned_composition_state_authority(payload, version=1)


def test_owned_state_authority_names_an_explicit_null_encoding_of_an_absent_field() -> None:
    payload = _authority_payload()
    payload["nodes"][0]["condition"] = None

    with pytest.raises(AuditIntegrityError, match="encodes an absent optional field as an explicit null"):
        restore_owned_composition_state_authority(payload, version=1)


@pytest.mark.parametrize(
    "node",
    [
        pytest.param(_coalesce_node(), id="coalesce_without_merge_or_policy"),
        pytest.param(
            _coalesce_node(id="union_rows", node_type="row_union", merge="union", policy="require_all"),
            id="row_union_with_list_branches",
        ),
    ],
)
def test_owned_state_authority_names_normalisation_drift_for_a_pre_normalisation_payload(node: dict[str, Any]) -> None:
    payload = _authority_payload()
    payload["nodes"].append(node)

    with pytest.raises(
        AuditIntegrityError,
        match="does not round-trip under the current composer state normalisation",
    ):
        restore_owned_composition_state_authority(payload, version=1)


@pytest.mark.parametrize(
    "branches",
    [pytest.param([1, 2], id="int_branch_aliases"), pytest.param([], id="empty_branch_list")],
)
def test_owned_state_authority_still_rejects_non_authored_row_union_branch_shapes(branches: list[Any]) -> None:
    payload = _authority_payload()
    payload["nodes"].append(_coalesce_node(id="union_rows", node_type="row_union", branches=branches))

    with pytest.raises(AuditIntegrityError, match="owned composition-state"):
        restore_owned_composition_state_authority(payload, version=1)


def test_owned_state_authority_reports_drift_when_removing_the_null_would_not_help() -> None:
    """A payload that is BOTH null-shaped and stale must report as stale.

    ``merge``/``policy`` written out as null is a written-out absence, but
    removing it leaves a coalesce that still predates the runtime-default
    normalisation. Naming the null here would hand the operator a remedy that
    resubmits into a second fatal rejection.
    """
    payload = _authority_payload()
    payload["nodes"].append(_coalesce_node(merge=None, policy=None))

    with pytest.raises(
        AuditIntegrityError,
        match="does not round-trip under the current composer state normalisation",
    ):
        restore_owned_composition_state_authority(payload, version=1)


def test_owned_state_authority_reports_drift_when_a_null_and_a_stale_node_are_both_present() -> None:
    """The null class is claimed for the payload, not for one sub-object.

    A removable null on one node alongside an unrelated stale node is still a
    quarantine-class payload; the null is not the reason it cannot restore.
    """
    payload = _authority_payload()
    payload["nodes"][0]["condition"] = None
    payload["nodes"].append(_coalesce_node())

    with pytest.raises(
        AuditIntegrityError,
        match="does not round-trip under the current composer state normalisation",
    ):
        restore_owned_composition_state_authority(payload, version=1)


def test_owned_state_authority_rejects_a_source_name_that_is_not_an_exact_string() -> None:
    payload = _authority_payload()
    payload["sources"][1] = payload["sources"].pop("source_1")

    with pytest.raises(AuditIntegrityError, match="owned composition-state authority sections are malformed"):
        restore_owned_composition_state_authority(payload, version=1)
