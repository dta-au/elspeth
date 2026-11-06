"""Order-binding regressions for the multi-source ``sources`` map.

``sources`` declaration order is runtime-semantic: it is the determinism
anchor for cross-source ingest order, and it decides which source is "first".
Composer authority hashes therefore project a top-level ``sources`` dict into
an ordered pair array, tagged ``composer.ordered-sources.v1``. Every dict is
projected, including ``{}`` and a single entry. The singular ``source`` field
is untouched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.core.canonical import stable_hash
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome, _DispatchOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, finish_success
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
    composition_content_hash,
)
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.tool_batch import BatchAccumulator, ToolBatchContext, ToolBatchCustody, ToolBatchProvenance, run_tool_batch
from elspeth.web.sessions.proposal_authority import _pipeline_private_arguments_hash
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer.conftest import (
    _empty_composition_state,
    _FakeChoice,
    _FakeFunction,
    _FakeLLMResponse,
    _FakeMessage,
    _FakeToolCall,
)

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
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
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


async def _dispatch_batch(
    service: ComposerServiceImpl,
    session_id: str,
    calls: tuple[tuple[str, str, str], ...],
    *,
    current_state_id: str | None = None,
) -> tuple[_DispatchOutcome, BufferingRecorder, list[dict[str, Any]]]:
    """Dispatch one admitted provider tool batch through ``tool_batch.run_tool_batch``.

    ``calls`` are ``(tool_call_id, name, raw_arguments)``. The context is built
    from the service exactly as Continuation builds it from its restored
    runtime (``continuation_runtime.build_runtime_tool_batch_context``); the
    shared dispatch owner is the code under test.
    """
    sessions = service._require_sessions_service()
    session_uuid = UUID(session_id)
    preferences = await sessions.get_composer_preferences(session_uuid)
    ceiling = ComposerMutationCeiling(session_id, "schema-gate-request", preferences.trust_mode)
    snapshot, policy_catalog = service._policy_context.build("phase3-test-user")
    response = _FakeLLMResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content=None,
                    tool_calls=[
                        _FakeToolCall(id=call_id, function=_FakeFunction(name=name, arguments=raw)) for call_id, name, raw in calls
                    ],
                )
            )
        ]
    )
    state = _empty_composition_state()
    recorder = BufferingRecorder()
    ctx = ToolBatchContext(
        session_tools=service._session_tools,
        provenance=ToolBatchProvenance(
            model_identifier=service._model, provider=service._availability.provider, skill_hash=service._composer_skill_hash
        ),
        custody=ToolBatchCustody(
            data_dir=service._data_dir,
            session_engine=service._session_engine,
            secret_service=service._secret_service,
            secret_wiring_policy=service._secret_wiring_policy,
            max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
        ),
        redaction_telemetry=service._redaction_telemetry,
        preflight=service._preflight,
        schema_disclosure=service._schema_disclosure,
        advisor_checkpoint=service._advisor_checkpoint,
        advisor_max_calls_per_compose=service._settings.composer_advisor_max_calls_per_compose,
        advisor_timeout_seconds=service._settings.composer_advisor_timeout_seconds,
        recorder=recorder,
        anti_anchor=AntiAnchorTracker(),
        discovery_cache={},
        runtime_preflight_cache=service._preflight.new_cache(),
        session_id=session_id,
        session_operation_context=None,
        user_id="phase3-test-user",
        user_message_id=None,
        user_message_content=None,
        current_state_id=current_state_id,
        actor="composer-web:user:phase3-test-user",
        initial_version=state.version,
        deadline=asyncio.get_running_loop().time() + 60,
        progress=None,
        session_scope=f"session:{session_id}",
        turn_sessions_service=sessions,
        turn_session_uuid=session_uuid,
        turn_preferences=preferences,
        mutation_ceiling=ceiling,
        review_required=not ceiling.permits_auto(preferences.trust_mode),
        cancellation_requested=asyncio.Event(),
        plugin_snapshot=snapshot,
        policy_catalog=policy_catalog,
        tool_contract_dialect=service._planner_dialect,
        session_operation_authority=sessions.session_operation_authority,
    )
    acc = BatchAccumulator(state=state, last_validation=None, last_runtime_preflight=None, advisor_calls_used=0)
    llm_messages: list[dict[str, Any]] = []
    with fenced_operation_context(sessions._engine, session_id) as context:
        outcome, _advisor_calls = await run_tool_batch(
            call_model=_CallModelOutcome(completion=_admit_composer_llm_completion(response)),
            ctx=replace(ctx, session_operation_context=context),
            acc=acc,
            llm_messages=llm_messages,
        )
    return outcome, recorder, llm_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [None, "csv"], ids=["null", "string"])
async def test_planner_sources_argument_error_persists_through_the_compose_loop(
    spec: object,
    fake_composer_service: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    """Characterization: an argument-error ``sources`` spec persists instead of raising.

    Dispatched through the shared owner ``tool_batch.run_tool_batch`` that
    Continuation's batch phase drives.
    """
    arguments = {
        "sources": {"main": spec},
        "nodes": [],
        "edges": [],
        "outputs": [{"sink_name": "out", "plugin": "json", "options": {"path": "o.json", "schema": {"mode": "observed"}}}],
    }
    _outcome, recorder, _messages = await _dispatch_batch(
        fake_composer_service, result_session_id, (("call_sp", "set_pipeline", json.dumps({"pipeline": arguments})),)
    )

    invocations = [inv for inv in recorder.invocations if inv.tool_name == "set_pipeline"]
    assert len(invocations) == 1
    assert invocations[0].status.value == "arg_error"
    _content, envelope = redacted_tool_invocation_content_and_envelope(invocations[0])
    persisted = envelope["invocation"]
    assert type(persisted) is dict
    assert persisted["status"] == "arg_error"
    assert persisted["authority_arguments_canonical"] is not None
