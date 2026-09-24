"""Order-binding regressions for Composer coalesce authority hashes.

Coalesce ``branches`` in mapping form are order-semantic at runtime: the
engine carries ``branch_order`` into the coalesce node unconditionally, and a
``merge="union"`` coalesce resolves field collisions in declaration order.
The composer cannot set ``union_collision_policy``, so every composer coalesce
runs under the default ``last_wins`` and its branch order decides the
collision winner. These tests vary the arrival ``policy`` and the ``merge``
mode; they never vary the collision rule, because the composer cannot.

Argument payloads use the singular ``source`` field and carry no ``sources``
key, so the multi-source projection cannot move this file's canonicals.
"""

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
from elspeth.web.composer.authority_hashing import (
    composer_authority_canonical_json,
    composer_authority_hash,
    project_composer_authority_payload,
    restore_composer_authority_payload,
)
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PipelineProposal,
    PlannerSurface,
    composition_content_hash,
)
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import (
    _composition_state_data_content_hash,
    _pipeline_audit_payload_hash,
    _pipeline_private_arguments_hash,
)
from tests.unit.web.composer.conftest import _fake_llm_response, _FakeComposeLLM

_COALESCE_TAG = "composer.coalesce-ordered-branches.v1"
_ROW_UNION_TAG = "composer.row-union-ordered-branches.v1"

_SOURCE: dict[str, Any] = {
    "plugin": "csv",
    "on_success": "rows",
    "options": {"path": "cases.csv"},
    "on_validation_failure": "discard",
}

# T0 golden (captured at the base commit, before any coalesce projection):
# a row_union set_pipeline payload with mapping branches and no ``sources``
# key. The row_union projection must stay byte-identical.
_ROW_UNION_GOLDEN_ARGS: dict[str, Any] = {
    "source": _SOURCE,
    "nodes": [
        {
            "id": "union",
            "node_type": "row_union",
            "plugin": None,
            "input": "a_in",
            "on_success": "union_out",
            "on_error": None,
            "options": {},
            "branches": {"c": "c_in", "a": "a_in", "b": "b_in"},
            "timeout_seconds": 30.0,
        }
    ],
    "edges": [],
    "outputs": [],
}
_ROW_UNION_GOLDEN_CANONICAL = (
    '{"edges":[],"nodes":[{"branches":{"items":[["c","c_in"],["a","a_in"],["b","b_in"]],'
    '"schema":"composer.row-union-ordered-branches.v1"},"id":"union","input":"a_in","node_type":"row_union",'
    '"on_error":null,"on_success":"union_out","options":{},"plugin":null,"timeout_seconds":30}],"outputs":[],'
    '"source":{"on_success":"rows","on_validation_failure":"discard","options":{"path":"cases.csv"},"plugin":"csv"}}'
)

_POLICIES = ("require_all", "best_effort", "first")
_MERGES = ("union", "nested", "select")


def _branches(order: Sequence[str]) -> dict[str, str]:
    connections = {"a": "a_in", "b": "b_in", "c": "c_in"}
    return {alias: connections[alias] for alias in order}


def _coalesce_node_payload(branches: object, *, policy: str = "require_all", merge: str = "union") -> dict[str, Any]:
    return {
        "id": "merge",
        "node_type": "coalesce",
        "plugin": None,
        "input": "a_in",
        "on_success": "merge_out",
        "on_error": None,
        "options": {},
        "branches": branches,
        "policy": policy,
        "merge": merge,
    }


def _pipeline(order: Sequence[str], *, policy: str = "require_all", merge: str = "union") -> dict[str, Any]:
    return {
        "source": dict(_SOURCE),
        "nodes": [_coalesce_node_payload(_branches(order), policy=policy, merge=merge)],
        "edges": [],
        "outputs": [],
    }


def _node(order: Sequence[str], *, policy: str = "require_all", merge: str = "union") -> NodeSpec:
    return NodeSpec(
        id="merge",
        node_type="coalesce",
        plugin=None,
        input="a_in",
        on_success="merge_out",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=_branches(order),
        policy=policy,
        merge=merge,
        timeout_seconds=30.0,
    )


def _state(order: Sequence[str], *, policy: str = "require_all", merge: str = "union") -> CompositionState:
    return CompositionState(
        sources={},
        nodes=(_node(order, policy=policy, merge=merge),),
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


def _proposal(pipeline: dict[str, Any]) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline=pipeline,
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
        "pipeline_content_hash": stable_hash({"state": "coalesce"}),
    }


def _persisted_envelope(pipeline: dict[str, Any], call_id: str) -> dict[str, Any]:
    audit = begin_dispatch(call_id, "set_pipeline", pipeline, version_before=1, actor="test")
    invocation = finish_success(audit, result_payload=_dispatch_result(), version_after=2)
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    return envelope


def _projection(items: list[list[object]], *, schema: str = _COALESCE_TAG) -> dict[str, object]:
    return {"schema": schema, "items": items}


def _projected_payload(node_type: str, branches: object) -> dict[str, Any]:
    node = _coalesce_node_payload(branches)
    node["node_type"] = node_type
    return {"source": dict(_SOURCE), "nodes": [node], "edges": [], "outputs": []}


# --- 1-4: a non-first reorder moves every authority hash ---------------------


@pytest.mark.parametrize("merge", _MERGES)
@pytest.mark.parametrize("policy", _POLICIES)
def test_non_first_coalesce_reorder_changes_composition_content_hash(policy: str, merge: str) -> None:
    assert composition_content_hash(_state(("a", "b", "c"), policy=policy, merge=merge)) != composition_content_hash(
        _state(("a", "c", "b"), policy=policy, merge=merge)
    )


def test_non_first_coalesce_reorder_changes_pipeline_draft_hash() -> None:
    assert _proposal(_pipeline(("a", "b", "c"))).draft_hash != _proposal(_pipeline(("a", "c", "b"))).draft_hash


def test_non_first_coalesce_reorder_changes_private_and_state_data_hashes() -> None:
    abc = _pipeline(("a", "b", "c"))
    acb = _pipeline(("a", "c", "b"))
    assert composer_authority_hash(abc) != composer_authority_hash(acb)
    assert _pipeline_private_arguments_hash(abc) != _pipeline_private_arguments_hash(acb)
    assert _pipeline_audit_payload_hash(
        summary="summary",
        rationale="rationale",
        affects=("graph",),
        arguments_redacted_json=abc,
    ) != _pipeline_audit_payload_hash(
        summary="summary",
        rationale="rationale",
        affects=("graph",),
        arguments_redacted_json=acb,
    )
    assert _composition_state_data_content_hash(_state_data(("a", "b", "c"))) != _composition_state_data_content_hash(
        _state_data(("a", "c", "b"))
    )


def test_non_first_coalesce_reorder_changes_set_pipeline_dispatch_binding_without_mutating_arguments() -> None:
    abc = _pipeline(("a", "b", "c"))
    acb = _pipeline(("a", "c", "b"))
    abc_before = _pipeline(("a", "b", "c"))
    acb_before = _pipeline(("a", "c", "b"))

    abc_audit = begin_dispatch("call-abc", "set_pipeline", abc, version_before=1, actor="test")
    acb_audit = begin_dispatch("call-acb", "set_pipeline", acb, version_before=1, actor="test")
    rebound_abc = rebind_dispatch_arguments(abc_audit, abc)
    rebound_acb = rebind_dispatch_arguments(acb_audit, acb)

    assert abc_audit.arguments_hash == acb_audit.arguments_hash
    assert abc_audit.authority_arguments_hash != acb_audit.authority_arguments_hash
    assert abc_audit.authority_arguments_canonical != acb_audit.authority_arguments_canonical
    assert rebound_abc.authority_arguments_hash != rebound_acb.authority_arguments_hash
    assert rebound_abc.authority_arguments_canonical != rebound_acb.authority_arguments_canonical
    assert abc_audit.binding_arguments_hash == composer_authority_hash(abc)
    assert acb_audit.binding_arguments_hash == composer_authority_hash(acb)
    assert abc == abc_before
    assert acb == acb_before


# --- 5-6a: persisted envelope shape and tamper rejection ---------------------


def test_set_pipeline_redacted_storage_keeps_coalesce_map_shape_and_binds_projection() -> None:
    pipeline = _pipeline(("a", "c", "b"))
    envelope = _persisted_envelope(pipeline, "call-redacted")
    persisted = envelope["invocation"]
    assert type(persisted) is dict

    redacted = redact_tool_call_arguments("set_pipeline", pipeline, telemetry=NoopRedactionTelemetry())
    expected_canonical = canonical_json(redacted)
    assert persisted["arguments_canonical"] == expected_canonical
    assert json.loads(expected_canonical)["nodes"][0]["branches"] == {"a": "a_in", "b": "b_in", "c": "c_in"}
    authority = json.loads(persisted["authority_arguments_canonical"])
    assert authority["nodes"][0]["branches"] == _projection([["a", "a_in"], ["c", "c_in"], ["b", "b_in"]])
    assert persisted["authority_arguments_hash"] == hashlib.sha256(persisted["authority_arguments_canonical"].encode()).hexdigest()

    binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)
    assert binding.arguments_hash == composer_authority_hash(redacted)


def test_persisted_coalesce_binding_rejects_recomputed_connection_tampering() -> None:
    """Characterization: a value tamper is caught by the generic-canonical comparison.

    The tamper edits the persisted authority canonical as text, so it applies
    to the projected pair array and to a plain map alike; the envelope hash is
    recomputed so only the restore comparison can catch it.
    """
    envelope = _persisted_envelope(_pipeline(("a", "c", "b")), "call-tampered")
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    authority_canonical = persisted["authority_arguments_canonical"]
    assert type(authority_canonical) is str
    assert authority_canonical.count('"c_in"') == 1
    tampered_canonical = authority_canonical.replace('"c_in"', '"different_connection"')
    persisted["authority_arguments_canonical"] = tampered_canonical
    persisted["authority_arguments_hash"] = hashlib.sha256(tampered_canonical.encode()).hexdigest()

    with pytest.raises(AuditIntegrityError, match="authority projection"):
        PipelineDispatchAuditBinding.from_persisted_envelope(envelope)


# --- 7, 7c: restore is strict about the projection's structure ---------------


@pytest.mark.parametrize(
    ("case", "branches"),
    [
        ("plain_map", {"a": "a_in", "b": "b_in"}),
        ("row_union_tag", _projection([["a", "a_in"]], schema=_ROW_UNION_TAG)),
        ("missing_tag", {"items": [["a", "a_in"]]}),
        ("extra_key", {"schema": _COALESCE_TAG, "items": [["a", "a_in"]], "extra": 1}),
        ("items_not_list", {"schema": _COALESCE_TAG, "items": {"a": "a_in"}}),
        ("duplicate_alias", _projection([["a", "a_in"], ["a", "b_in"]])),
        ("non_str_alias", _projection([[1, "a_in"]])),
        ("non_list_item", _projection([{"a": "a_in"}])),
        ("three_item_entry", _projection([["a", "a_in", "extra"]])),
        ("one_item_entry", _projection([["a"]])),
    ],
)
def test_restore_rejects_malformed_coalesce_projection(case: str, branches: object) -> None:
    with pytest.raises(ValueError, match=r"^coalesce authority projection branch"):
        restore_composer_authority_payload(_projected_payload("coalesce", branches))


def test_restore_rejects_coalesce_tag_on_row_union_node() -> None:
    """Characterization: the row_union arm already rejects any foreign tag."""
    with pytest.raises(ValueError, match=r"^row-union authority projection branches are malformed$"):
        restore_composer_authority_payload(_projected_payload("row_union", _projection([["a", "a_in"]])))


@pytest.mark.parametrize(
    ("case", "branches", "message"),
    [
        ("plain_map", {"a": "a_in", "b": "b_in"}, "branches are malformed"),
        ("missing_tag", {"items": [["a", "a_in"]]}, "branches are malformed"),
        ("extra_key", {"schema": _ROW_UNION_TAG, "items": [["a", "a_in"]], "extra": 1}, "branches are malformed"),
        ("items_not_list", {"schema": _ROW_UNION_TAG, "items": {"a": "a_in"}}, "branches are malformed"),
        ("duplicate_alias", _projection([["a", "a_in"], ["a", "b_in"]], schema=_ROW_UNION_TAG), "branch item is malformed"),
        ("non_str_alias", _projection([[1, "a_in"]], schema=_ROW_UNION_TAG), "branch item is malformed"),
        ("non_list_item", _projection([{"a": "a_in"}], schema=_ROW_UNION_TAG), "branch item is malformed"),
        ("three_item_entry", _projection([["a", "a_in", "extra"]], schema=_ROW_UNION_TAG), "branch item is malformed"),
        ("one_item_entry", _projection([["a"]], schema=_ROW_UNION_TAG), "branch item is malformed"),
    ],
)
def test_restore_rejects_malformed_row_union_projection(case: str, branches: object, message: str) -> None:
    """Characterization: the row_union arm fails closed on the same structures as coalesce.

    The row_union arm rejected these before the shared helper existed, with
    these exact messages; the duplicate-alias case pins R2 condition 3 for
    row_union, which no stored-projection test covered.
    """
    with pytest.raises(ValueError, match=rf"^row-union authority projection {message}$"):
        restore_composer_authority_payload(_projected_payload("row_union", branches))


# --- 8-10: preimages that must not move; R2 conditions 1-2 -------------------


def test_list_form_coalesce_generic_and_authority_canonicals_are_byte_equal() -> None:
    """Characterization: list-form branches are already ordered; nothing is projected."""
    pipeline = {
        "source": dict(_SOURCE),
        "nodes": [_coalesce_node_payload(["right", "left"])],
        "edges": [],
        "outputs": [],
    }
    audit = begin_dispatch("call-list", "set_pipeline", pipeline, version_before=1, actor="test")
    assert audit.arguments_canonical == audit.authority_arguments_canonical
    assert composer_authority_canonical_json(pipeline) == canonical_json(pipeline)


def test_row_union_projection_is_byte_identical_to_the_t0_golden() -> None:
    """Characterization: the row_union tag and projection shape did not move."""
    assert "sources" not in _ROW_UNION_GOLDEN_ARGS
    assert composer_authority_canonical_json(_ROW_UNION_GOLDEN_ARGS) == _ROW_UNION_GOLDEN_CANONICAL


@pytest.mark.parametrize("node_type", ["coalesce", "row_union"])
def test_restore_of_projection_is_lossless_and_keeps_branch_order(node_type: str) -> None:
    """Characterization of R2 conditions 1 and 2 on the audit-internal surface."""
    payload = _projected_payload(node_type, {"c": "c_in", "a": "a_in", "b": "b_in"})
    restored = restore_composer_authority_payload(project_composer_authority_payload(payload))
    assert restored == payload
    assert list(restored["nodes"][0]["branches"]) == ["c", "a", "b"]


def test_restore_of_empty_projected_coalesce_map_is_an_empty_map() -> None:
    """R2 condition 5 (reject ``[]``) is a wire-decode rule; an empty projected map is legal here."""
    payload = _projected_payload("coalesce", {})
    projected = project_composer_authority_payload(payload)
    assert projected["nodes"][0]["branches"] == _projection([])
    assert restore_composer_authority_payload(projected) == payload


# --- 11: the runtime preflight cache key -------------------------------------


def test_runtime_preflight_key_differs_for_coalesce_reorder(fake_composer_service: ComposerServiceImpl) -> None:
    abc = fake_composer_service._runtime_preflight_key(_state(("a", "b", "c")), session_scope="s", plugin_snapshot=None)
    acb = fake_composer_service._runtime_preflight_key(_state(("a", "c", "b")), session_scope="s", plugin_snapshot=None)
    assert abc.state_version == acb.state_version
    assert abc.state_content_hash != acb.state_content_hash
    assert abc != acb


# --- 12-13: restore is lossless about branch values (D11) --------------------


@pytest.mark.parametrize("node_type", ["coalesce", "row_union"])
@pytest.mark.parametrize("value", [1, None, {"nested": "object"}], ids=["int", "null", "object"])
def test_restore_accepts_any_json_branch_value(node_type: str, value: object) -> None:
    payload = _projected_payload(node_type, {"zeta": value, "alpha": "alpha_in"})
    restored = restore_composer_authority_payload(project_composer_authority_payload(payload))
    assert restored == payload
    assert list(restored["nodes"][0]["branches"]) == ["zeta", "alpha"]


_LOOP_SOURCE = {"plugin": "csv", "on_success": "rows", "options": {"path": "x.csv", "schema": {"mode": "observed"}}}
_LOOP_OUTPUTS = [{"sink_name": "out", "plugin": "json", "options": {"path": "o.json", "schema": {"mode": "observed"}}}]
_LOOP_GATE = {"id": "g", "node_type": "gate", "input": "rows", "condition": "True", "fork_to": ["zb", "ab"]}


def _loop_pipeline(node: dict[str, Any]) -> dict[str, Any]:
    return {"source": _LOOP_SOURCE, "nodes": [_LOOP_GATE, node], "edges": [], "outputs": _LOOP_OUTPUTS}


_LOOP_ARGUMENT_ERROR_CASES = {
    "row_union_int_value": _loop_pipeline(
        {"id": "c", "node_type": "row_union", "input": "zb", "branches": {"zeta": 1, "alpha": "ab"}, "on_success": "out"}
    ),
    "coalesce_null_value": _loop_pipeline(
        {"id": "c", "node_type": "coalesce", "input": "zb", "branches": {"zeta": None, "alpha": "ab"}, "on_success": "out"}
    ),
    "coalesce_list_node_type": _loop_pipeline(
        {"id": "c", "node_type": [], "input": "zb", "branches": {"zeta": "zb", "alpha": "ab"}, "on_success": "out"}
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_LOOP_ARGUMENT_ERROR_CASES))
async def test_planner_argument_error_persists_through_the_compose_loop(
    case: str,
    fake_composer_service: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """A malformed planner ``set_pipeline`` is an argument error that persists, not a crash."""
    llm = _FakeComposeLLM(
        (
            _fake_llm_response(tool_calls=({"id": "call_sp", "name": "set_pipeline", "arguments": _LOOP_ARGUMENT_ERROR_CASES[case]},)),
            _fake_llm_response(content="Done."),
        )
    )
    result = await fake_composer_service._run_one_turn_for_test(llm=llm, session_id=result_session_id)

    invocations = [inv for inv in result.tool_invocations if inv.tool_name == "set_pipeline"]
    assert len(invocations) == 1
    invocation = invocations[0]
    assert invocation.status.value == "arg_error"
    assert invocation.authority_arguments_canonical is not None
    # Persistence validates both argument bindings and restores the authority
    # projection (audit_storage._validated_invocation_arguments). The dispatch
    # binding reader only admits successful dispatches, so it is not the reader.
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    assert persisted["status"] == "arg_error"
    assert persisted["authority_arguments_canonical"] is not None


# --- 14: a malformed discriminant passes through unprojected -----------------


@pytest.mark.parametrize("node_type", [[], {}, 1], ids=["list", "dict", "int"])
def test_non_str_node_type_passes_through_projection_and_restore_unprojected(node_type: object) -> None:
    """Characterization: a planner ``node_type`` of the wrong JSON type never crashes auditing."""
    payload = _projected_payload("coalesce", {"zeta": "zeta_in", "alpha": "alpha_in"})
    payload["nodes"][0]["node_type"] = node_type

    projected = project_composer_authority_payload(payload)
    assert projected["nodes"][0]["branches"] == {"zeta": "zeta_in", "alpha": "alpha_in"}
    assert restore_composer_authority_payload(projected) == payload
    audit = begin_dispatch("call-bad-node-type", "set_pipeline", payload, version_before=1, actor="test")
    assert audit.authority_arguments_canonical == audit.arguments_canonical
