"""Order-binding regressions for the multi-source ``sources`` map.

``sources`` declaration order is runtime-semantic: it is the determinism
anchor for cross-source ingest order, and it decides which source is "first".
Composer authority hashes therefore project a top-level ``sources`` dict into
an ordered pair array, tagged ``composer.ordered-sources.v1``. Every dict is
projected, including ``{}`` and a single entry. The singular ``source`` field
is untouched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from elspeth.core.canonical import stable_hash
from elspeth.web.composer.audit import begin_dispatch, finish_success
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
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.sessions.service import _pipeline_private_arguments_hash
from tests.unit.web.composer.conftest import _fake_llm_response, _FakeComposeLLM

_SOURCES_TAG = "composer.ordered-sources.v1"

# T0 golden (captured at the base commit): a set_pipeline payload using the
# singular ``source`` field and carrying no ``sources`` key, with a list-form
# coalesce. Nothing in it is projected, so its canonical must not move.
_SOURCES_FREE_GOLDEN_ARGS: dict[str, Any] = {
    "source": {
        "plugin": "csv",
        "on_success": "rows",
        "options": {"path": "cases.csv"},
        "on_validation_failure": "discard",
    },
    "nodes": [
        {
            "id": "merge",
            "node_type": "coalesce",
            "plugin": None,
            "input": "rows",
            "on_success": "out",
            "on_error": None,
            "options": {},
            "branches": ["right", "left"],
            "policy": "require_all",
            "merge": "union",
        }
    ],
    "edges": [],
    "outputs": [],
}
_SOURCES_FREE_GOLDEN_CANONICAL = (
    '{"edges":[],"nodes":[{"branches":["right","left"],"id":"merge","input":"rows","merge":"union",'
    '"node_type":"coalesce","on_error":null,"on_success":"out","options":{},"plugin":null,"policy":"require_all"}],'
    '"outputs":[],"source":{"on_success":"rows","on_validation_failure":"discard","options":{"path":"cases.csv"},'
    '"plugin":"csv"}}'
)


def _source_payload(name: str) -> dict[str, Any]:
    return {
        "plugin": "csv",
        "on_success": f"{name}_rows",
        "options": {"path": f"{name}.csv"},
        "on_validation_failure": "discard",
    }


def _pipeline(order: Sequence[str]) -> dict[str, Any]:
    return {
        "sources": {name: _source_payload(name) for name in order},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }


def _state(order: Sequence[str]) -> CompositionState:
    return CompositionState(
        sources={
            name: SourceSpec(
                plugin="csv",
                on_success=f"{name}_rows",
                options={"path": f"{name}.csv"},
                on_validation_failure="discard",
            )
            for name in order
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
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


def _with_sources(sources: object) -> dict[str, Any]:
    return {"sources": sources, "nodes": [], "edges": [], "outputs": []}


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
        "pipeline_content_hash": stable_hash({"state": "sources"}),
    }


# --- 1: a reorder moves every authority hash ---------------------------------


def test_sources_reorder_changes_composition_content_hash() -> None:
    assert composition_content_hash(_state(("alpha", "zeta"))) != composition_content_hash(_state(("zeta", "alpha")))


def test_sources_reorder_changes_draft_and_private_argument_hashes() -> None:
    az = _pipeline(("alpha", "zeta"))
    za = _pipeline(("zeta", "alpha"))
    assert composer_authority_hash(az) != composer_authority_hash(za)
    assert _proposal(az).draft_hash != _proposal(za).draft_hash
    assert _pipeline_private_arguments_hash(az) != _pipeline_private_arguments_hash(za)


def test_sources_reorder_changes_set_pipeline_dispatch_binding_without_mutating_arguments() -> None:
    az = _pipeline(("alpha", "zeta"))
    za = _pipeline(("zeta", "alpha"))
    az_audit = begin_dispatch("call-az", "set_pipeline", az, version_before=1, actor="test")
    za_audit = begin_dispatch("call-za", "set_pipeline", za, version_before=1, actor="test")

    assert az_audit.arguments_hash == za_audit.arguments_hash
    assert az_audit.authority_arguments_hash != za_audit.authority_arguments_hash
    assert az == _pipeline(("alpha", "zeta"))
    assert list(za["sources"]) == ["zeta", "alpha"]


def test_persisted_sources_envelope_binds_the_projection_and_round_trips() -> None:
    pipeline = _pipeline(("zeta", "alpha"))
    audit = begin_dispatch("call-sources", "set_pipeline", pipeline, version_before=1, actor="test")
    invocation = finish_success(audit, result_payload=_dispatch_result(), version_after=2)
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    assert list(json.loads(persisted["arguments_canonical"])["sources"]) == ["alpha", "zeta"]
    authority_sources = json.loads(persisted["authority_arguments_canonical"])["sources"]
    assert authority_sources["schema"] == _SOURCES_TAG
    assert [item[0] for item in authority_sources["items"]] == ["zeta", "alpha"]

    binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)
    assert binding.arguments_hash == persisted["authority_arguments_hash"]


# --- 2: restore is strict about the projection's structure -------------------


@pytest.mark.parametrize(
    ("case", "sources"),
    [
        ("plain_map", {"main": _source_payload("main")}),
        ("row_union_tag", {"schema": "composer.row-union-ordered-branches.v1", "items": []}),
        ("coalesce_tag", {"schema": "composer.coalesce-ordered-branches.v1", "items": []}),
        ("missing_tag", {"items": []}),
        ("extra_key", {"schema": _SOURCES_TAG, "items": [], "extra": 1}),
        ("items_not_list", {"schema": _SOURCES_TAG, "items": {"main": {}}}),
        ("duplicate_name", {"schema": _SOURCES_TAG, "items": [["main", {}], ["main", {}]]}),
        ("non_str_name", {"schema": _SOURCES_TAG, "items": [[1, {}]]}),
        ("non_list_item", {"schema": _SOURCES_TAG, "items": [{"main": {}}]}),
        ("three_item_entry", {"schema": _SOURCES_TAG, "items": [["main", {}, "extra"]]}),
    ],
)
def test_restore_rejects_malformed_sources_projection(case: str, sources: object) -> None:
    with pytest.raises(ValueError, match=r"^sources authority projection"):
        restore_composer_authority_payload(_with_sources(sources))


# --- 3-4: preimages that must not move; R2 conditions 1-2 --------------------


def test_sources_free_payload_canonical_is_byte_identical_to_the_t0_golden() -> None:
    """Characterization: a payload with only the singular ``source`` is not projected."""
    assert "sources" not in _SOURCES_FREE_GOLDEN_ARGS
    assert composer_authority_canonical_json(_SOURCES_FREE_GOLDEN_ARGS) == _SOURCES_FREE_GOLDEN_CANONICAL


@pytest.mark.parametrize("order", [(), ("main",), ("zeta", "alpha", "mid")], ids=["empty", "single", "three"])
def test_restore_of_sources_projection_is_lossless_and_keeps_order(order: tuple[str, ...]) -> None:
    """Characterization of R2 conditions 1 and 2, including ``{}`` and a single entry."""
    pipeline = _pipeline(order)
    projected = project_composer_authority_payload(pipeline)
    restored = restore_composer_authority_payload(projected)
    assert restored == pipeline
    assert list(restored["sources"]) == list(order)


# --- 5: restore is lossless about source specs (D11) -------------------------


@pytest.mark.parametrize("spec", [None, "csv", 1], ids=["null", "string", "int"])
def test_restore_accepts_any_json_source_spec(spec: object) -> None:
    pipeline = _with_sources({"main": spec, "other": _source_payload("other")})
    restored = restore_composer_authority_payload(project_composer_authority_payload(pipeline))
    assert restored == pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [None, "csv"], ids=["null", "string"])
async def test_planner_sources_argument_error_persists_through_the_compose_loop(
    spec: object,
    fake_composer_service: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """Characterization: an argument-error ``sources`` spec persists instead of raising."""
    arguments = {
        "sources": {"main": spec},
        "nodes": [],
        "edges": [],
        "outputs": [{"sink_name": "out", "plugin": "json", "options": {"path": "o.json", "schema": {"mode": "observed"}}}],
    }
    llm = _FakeComposeLLM(
        (
            _fake_llm_response(tool_calls=({"id": "call_sp", "name": "set_pipeline", "arguments": arguments},)),
            _fake_llm_response(content="Done."),
        )
    )
    result = await fake_composer_service._run_one_turn_for_test(llm=llm, session_id=result_session_id)

    invocations = [inv for inv in result.tool_invocations if inv.tool_name == "set_pipeline"]
    assert len(invocations) == 1
    assert invocations[0].status.value == "arg_error"
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocations[0])
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    assert persisted["status"] == "arg_error"
    assert persisted["authority_arguments_canonical"] is not None
