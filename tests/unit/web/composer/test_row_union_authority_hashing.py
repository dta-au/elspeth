"""Order-binding regressions for Composer row-union authority hashes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer.audit import begin_dispatch, finish_success, rebind_dispatch_arguments
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.authority_hashing import composer_authority_hash, project_composer_authority_payload
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PipelineProposal,
    PlannerSurface,
    composition_content_hash,
)
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import (
    _composition_state_data_content_hash,
    _pipeline_audit_payload_hash,
    _pipeline_private_arguments_hash,
)


def _branches(order: Sequence[str]) -> dict[str, str]:
    connections = {
        "a": "a_in",
        "b": "b_in",
        "c": "c_in",
    }
    return {alias: connections[alias] for alias in order}


def _pipeline(order: Sequence[str]) -> dict[str, Any]:
    return {
        "sources": {},
        "nodes": [
            {
                "id": "union",
                "node_type": "row_union",
                "plugin": None,
                "input": "a_in",
                "on_success": "union_out",
                "on_error": None,
                "options": {},
                "branches": _branches(order),
                "timeout_seconds": 30.0,
            }
        ],
        "edges": [],
        "outputs": [],
    }


def _node(order: Sequence[str]) -> NodeSpec:
    return NodeSpec(
        id="union",
        node_type="row_union",
        plugin=None,
        input="a_in",
        on_success="union_out",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=_branches(order),
        policy=None,
        merge=None,
        timeout_seconds=30.0,
    )


def _state(order: Sequence[str]) -> CompositionState:
    return CompositionState(
        sources={},
        nodes=(_node(order),),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _state_data(order: Sequence[str]) -> CompositionStateData:
    state = _state(order).to_dict()
    return CompositionStateData(
        sources=state["sources"],
        nodes=state["nodes"],
        edges=state["edges"],
        outputs=state["outputs"],
        metadata_=state["metadata"],
        is_valid=True,
    )


def _proposal(order: Sequence[str]) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline=_pipeline(order),
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.GUIDED_FULL,
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
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
        "affected_nodes": [],
        "version": 1,
        "pipeline_content_hash_schema": "composer.pipeline-dispatch-result.v1",
        "pipeline_content_hash": stable_hash({"state": "row-union"}),
    }


def test_non_first_row_union_reorder_changes_composition_content_hash() -> None:
    assert composition_content_hash(_state(("a", "b", "c"))) != composition_content_hash(_state(("a", "c", "b")))


def test_non_first_row_union_reorder_changes_pipeline_draft_hash() -> None:
    assert _proposal(("a", "b", "c")).draft_hash != _proposal(("a", "c", "b")).draft_hash


def test_non_first_row_union_reorder_changes_private_and_state_data_hashes() -> None:
    assert composer_authority_hash(_pipeline(("a", "b", "c"))) != composer_authority_hash(_pipeline(("a", "c", "b")))
    assert _pipeline_private_arguments_hash(_pipeline(("a", "b", "c"))) != _pipeline_private_arguments_hash(_pipeline(("a", "c", "b")))
    assert _pipeline_audit_payload_hash(
        summary="summary",
        rationale="rationale",
        affects=("graph",),
        arguments_redacted_json=_pipeline(("a", "b", "c")),
    ) != _pipeline_audit_payload_hash(
        summary="summary",
        rationale="rationale",
        affects=("graph",),
        arguments_redacted_json=_pipeline(("a", "c", "b")),
    )
    assert _composition_state_data_content_hash(_state_data(("a", "b", "c"))) != _composition_state_data_content_hash(
        _state_data(("a", "c", "b"))
    )


def test_non_first_row_union_reorder_changes_set_pipeline_dispatch_binding_without_mutating_arguments() -> None:
    abc = _pipeline(("a", "b", "c"))
    acb = _pipeline(("a", "c", "b"))
    abc_before = _pipeline(("a", "b", "c"))
    acb_before = _pipeline(("a", "c", "b"))

    abc_audit = begin_dispatch("call-abc", "set_pipeline", abc, version_before=1, actor="test")
    acb_audit = begin_dispatch("call-acb", "set_pipeline", acb, version_before=1, actor="test")
    rebound_abc = rebind_dispatch_arguments(abc_audit, abc)
    rebound_acb = rebind_dispatch_arguments(acb_audit, acb)

    assert abc_audit.arguments_canonical == canonical_json(abc)
    assert acb_audit.arguments_canonical == canonical_json(acb)
    assert abc_audit.arguments_hash == stable_hash(abc)
    assert acb_audit.arguments_hash == stable_hash(acb)
    assert abc_audit.arguments_hash == acb_audit.arguments_hash
    assert getattr(abc_audit, "authority_arguments_hash", None) != getattr(acb_audit, "authority_arguments_hash", None)
    assert getattr(abc_audit, "authority_arguments_canonical", None) != getattr(
        acb_audit,
        "authority_arguments_canonical",
        None,
    )
    assert getattr(rebound_abc, "authority_arguments_hash", None) != getattr(
        rebound_acb,
        "authority_arguments_hash",
        None,
    )
    assert getattr(rebound_abc, "authority_arguments_canonical", None) != getattr(
        rebound_acb,
        "authority_arguments_canonical",
        None,
    )
    assert getattr(abc_audit, "binding_arguments_hash", None) == composer_authority_hash(abc)
    assert getattr(acb_audit, "binding_arguments_hash", None) == composer_authority_hash(acb)
    assert getattr(rebound_abc, "binding_arguments_hash", None) == composer_authority_hash(abc)
    assert getattr(rebound_acb, "binding_arguments_hash", None) == composer_authority_hash(acb)

    invocation = finish_success(abc_audit, result_payload=_dispatch_result(), version_after=2)
    invocation_payload = invocation.to_dict()
    assert invocation_payload["arguments_canonical"] == canonical_json(abc)
    assert invocation_payload["arguments_hash"] == stable_hash(abc)
    assert invocation_payload.get("authority_arguments_canonical") == canonical_json(project_composer_authority_payload(abc))
    assert invocation_payload.get("authority_arguments_hash") == composer_authority_hash(abc)
    assert abc == abc_before
    assert acb == acb_before


def test_set_pipeline_redacted_storage_preserves_tool_shape_and_rebinds_authority_projection() -> None:
    pipeline = _pipeline(("a", "c", "b"))
    audit = begin_dispatch("call-redacted", "set_pipeline", pipeline, version_before=1, actor="test")
    invocation = finish_success(audit, result_payload=_dispatch_result(), version_after=2)
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    assert type(persisted) is dict

    redacted = redact_tool_call_arguments("set_pipeline", pipeline, telemetry=NoopRedactionTelemetry())
    expected_canonical = canonical_json(redacted)
    expected_authority_canonical = canonical_json(project_composer_authority_payload(redacted))
    assert persisted["arguments_canonical"] == expected_canonical
    assert persisted["arguments_hash"] == hashlib.sha256(expected_canonical.encode()).hexdigest()
    assert persisted.get("authority_arguments_canonical") == expected_authority_canonical
    assert persisted.get("authority_arguments_hash") == hashlib.sha256(expected_authority_canonical.encode()).hexdigest()
    assert json.loads(expected_canonical)["nodes"][0]["branches"] == {"a": "a_in", "b": "b_in", "c": "c_in"}

    binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)
    assert binding.arguments_hash == composer_authority_hash(redacted)


def test_persisted_set_pipeline_binding_normalizes_legacy_absent_inline_blob_default() -> None:
    """Validated legacy null-bearing rows rebind to the current semantic hash."""
    pipeline = {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "input.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    audit = begin_dispatch("call-legacy-inline-default", "set_pipeline", pipeline, version_before=1, actor="test")
    invocation = finish_success(audit, result_payload=_dispatch_result(), version_after=2)
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    assert type(persisted) is dict

    normalized_redacted = redact_tool_call_arguments(
        "set_pipeline",
        pipeline,
        telemetry=NoopRedactionTelemetry(),
    )
    legacy_redacted = json.loads(canonical_json(normalized_redacted))
    legacy_redacted["source"]["inline_blob"] = None
    legacy_canonical = canonical_json(legacy_redacted)
    legacy_authority_canonical = canonical_json(project_composer_authority_payload(legacy_redacted))
    persisted["arguments_canonical"] = legacy_canonical
    persisted["arguments_hash"] = hashlib.sha256(legacy_canonical.encode()).hexdigest()
    persisted["authority_arguments_canonical"] = legacy_authority_canonical
    persisted["authority_arguments_hash"] = hashlib.sha256(legacy_authority_canonical.encode()).hexdigest()

    binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)

    assert "inline_blob" not in normalized_redacted["source"]
    assert binding.arguments_hash == composer_authority_hash(normalized_redacted)
    assert binding.arguments_hash != composer_authority_hash(legacy_redacted)


def test_persisted_set_pipeline_binding_rejects_recomputed_authority_member_tampering() -> None:
    pipeline = _pipeline(("a", "c", "b"))
    audit = begin_dispatch("call-tampered", "set_pipeline", pipeline, version_before=1, actor="test")
    invocation = finish_success(audit, result_payload=_dispatch_result(), version_after=2)
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    authority_canonical = persisted.get("authority_arguments_canonical")
    assert type(authority_canonical) is str
    tampered = json.loads(authority_canonical)
    tampered["nodes"][0]["branches"]["items"][1][1] = "different_connection"
    tampered_canonical = canonical_json(tampered)
    persisted["authority_arguments_canonical"] = tampered_canonical
    persisted["authority_arguments_hash"] = hashlib.sha256(tampered_canonical.encode()).hexdigest()

    with pytest.raises(AuditIntegrityError, match="authority projection"):
        PipelineDispatchAuditBinding.from_persisted_envelope(envelope)


def test_set_pipeline_list_branches_round_trips_through_generic_and_authority_audit_pairs() -> None:
    pipeline = _pipeline(("a", "b", "c"))
    pipeline["nodes"][0]["branches"] = ["a_in", "b_in"]
    audit = begin_dispatch("call-list-branches", "set_pipeline", pipeline, version_before=1, actor="test")
    invocation = finish_success(audit, result_payload=_dispatch_result(), version_after=1)

    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    assert json.loads(persisted["arguments_canonical"])["nodes"][0]["branches"] == ["a_in", "b_in"]
    assert persisted["arguments_canonical"] == persisted["authority_arguments_canonical"]
    binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)
    assert binding.arguments_hash == persisted["authority_arguments_hash"]
