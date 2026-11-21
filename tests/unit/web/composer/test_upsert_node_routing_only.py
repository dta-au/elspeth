"""``upsert_node(routing_only=true)``: rewire an existing transform without re-sending it.

The call changes only ``input``, ``on_success`` and ``on_error``. The stored
plugin, description and authored options stay byte-identical (a file-held
prompt included); the server copies them and authors nothing. Review
reconciliation runs as for any node write, and the receipt names every review
the run gate now waits on that it did not wait on before. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import batch_dependencies
from elspeth.web.composer import skills as skills_package
from elspeth.web.composer import tools as tools_package
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.proposals import build_tool_proposal_summary
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.required_controls import wire_required_controls_state
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.skills import load_skill
from elspeth.web.composer.state import CompositionState, EdgeSpec, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolResult, get_tool_definitions, transforms
from elspeth.web.composer.tools import execute_tool as production_execute_tool
from elspeth.web.composer.tools._common import (
    _DUPLICATE_CONSUMER_REASON,
    _options_with_default_prompt_template_review,
    _serialize_set_pipeline_node,
)
from elspeth.web.composer.tools.transforms import _UpsertNodeArgumentsModel
from elspeth.web.interpretation_state import (
    AUTHORING_METADATA_OPTION_KEYS,
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SHIELD_USER_TERM,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    InterpretationReviewSite,
    approved_prompt_artifact_hash_from_options,
    pending_execution_interpretation_sites,
    pipeline_decision_artifact_hash,
    prompt_review_draft_from_options,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes.composer import proposals as proposal_routes
from tests.helpers.prompt_review import reviewed_anchor_kwargs
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer import _fork_facts_graph as fork_graph
from tests.unit.web.composer.test_planner_authoring_aids import _guardrail_profile_view
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_service import _make_llm_response, _make_settings
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool
from tests.unit.web.composer.test_transform_tools_review_and_guard_parity import _node, _reviewed_shield_state
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store_fixture
from tests.unit.web.coordination.test_graph_recovery_persistence import begin
from tests.unit.web.sessions import test_approved_file_prompt_keeps_guarantees as file_prompt
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state
from tests.unit.web.sessions.test_composer_recovery_security import _checkpoint
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.test_interpretation_state import _web_scrape

blob_env = file_prompt.blob_env

# Stated here rather than imported, so the tests do not take the handler's word for them.
_ROUTING_FIELDS = frozenset({"id", "node_type", "input", "on_success", "on_error", "routing_only"})
# Server-owned option keys: review rows, prompt parts, source provenance, and the approval
# hash every tool write re-derives. Everything else in options was authored.
_SERVER_OWNED_OPTION_KEYS = AUTHORING_METADATA_OPTION_KEYS | {"approved_prompt_artifact_hash"}


def _rewire(state: CompositionState, node_id: str, **routes: Any) -> ToolResult:
    arguments: dict[str, Any] = {"id": node_id, "node_type": "transform", "routing_only": True}
    arguments["input"] = routes.pop("input", _node(state, node_id).input)
    arguments.update(routes)
    return execute_tool("upsert_node", arguments, state, _mock_catalog())


def _authored(options: Any) -> list[tuple[str, str]]:
    """Authored options in stored order, each value as JSON bytes."""
    return [
        (key, json.dumps(deep_thaw(value), ensure_ascii=False)) for key, value in options.items() if key not in _SERVER_OWNED_OPTION_KEYS
    ]


def _review_rows(options: Any) -> str:
    return json.dumps(deep_thaw(options[INTERPRETATION_REQUIREMENTS_KEY]), ensure_ascii=False)


def _refusal(result: ToolResult) -> tuple[str | None, str]:
    entry = result.validation.errors[0]
    return entry.error_code, entry.message


def _wire(result: ToolResult) -> dict[str, Any]:
    return json.loads(json.dumps(result.to_dict()))


def _data(result: ToolResult) -> Any:
    """The tool's ``data`` as the model reads it (the result holds it frozen)."""
    return _wire(result)["data"]


def _shield_status(state: CompositionState, node_id: str) -> str:
    rows = deep_thaw(_node(state, node_id).options)[INTERPRETATION_REQUIREMENTS_KEY]
    return next(row["status"] for row in rows if row["user_term"] == PROMPT_SHIELD_USER_TERM)


def _with_sinks(state: CompositionState, *names: str) -> CompositionState:
    for name in names:
        state = state.with_output(
            OutputSpec(name=name, plugin="json", options={"path": f"outputs/{name}.jsonl", "format": "jsonl"}, on_write_failure="discard")
        )
    return state


# ── T1 / T2: what changes and what stays ────────────────────────────────────


def test_input_rewire_keeps_every_other_field_and_returns_the_receipt_not_the_echo() -> None:
    state = _reviewed_shield_state()
    before = _node(state, "classify")

    result = _rewire(state, "classify", input="scraped")

    assert result.success, [entry.to_dict() for entry in result.validation.errors]
    after = _node(result.updated_state, "classify")
    assert (after.input, after.on_success, after.on_error) == ("scraped", before.on_success, before.on_error)
    assert replace(after, input=before.input, options=before.options) == before, "only input and server-owned option keys moved"
    assert after.plugin == before.plugin and after.description == before.description
    assert _authored(after.options) == _authored(before.options)
    wire = _wire(result)
    assert "applied_component" not in wire
    assert wire["data"] == {
        "routing_update": {"id": "classify", "input": "scraped", "on_success": "out", "on_error": "stop"},
        "options_preserved": True,
        "reopened_reviews": [{"component_id": "classify", "kind": "pipeline_decision", "user_term": PROMPT_SHIELD_USER_TERM}],
        "reopened_review_count": 1,
    }


def test_a_commit_tail_that_changed_an_authored_option_is_a_crash_not_a_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """``options_preserved: true`` is checked, not asserted by construction alone."""
    real = transforms.reconcile_authoritative_reviews

    def mutating(previous: CompositionState, proposed: CompositionState) -> CompositionState:
        reconciled = real(previous, proposed)
        node = _node(reconciled, "classify")
        return reconciled.with_node(replace(node, options={**node.options, "prompt_template": "Rewritten."}))

    monkeypatch.setattr(transforms, "reconcile_authoritative_reviews", mutating)
    with pytest.raises(AssertionError, match="changed an authored setting"):
        _rewire(_reviewed_shield_state(), "classify", input="scraped")


def test_omitted_routes_are_kept_and_supplied_routes_move_with_their_mirror_edge() -> None:
    state = _with_sinks(_reviewed_shield_state(), "out", "other", "errors")
    state = state.with_edge(EdgeSpec(id="e_classify_out", from_node="classify", to_node="out", edge_type="on_success", label=None))

    kept = _rewire(state, "classify", on_error="errors")
    assert kept.success, [entry.to_dict() for entry in kept.validation.errors]
    node = _node(kept.updated_state, "classify")
    assert (node.input, node.on_success, node.on_error) == ("inbound", "out", "errors")
    assert [(edge.id, edge.to_node) for edge in kept.updated_state.edges] == [("e_classify_out", "out")]

    moved = _rewire(kept.updated_state, "classify", on_success="other")
    assert moved.success, [entry.to_dict() for entry in moved.validation.errors]
    node = _node(moved.updated_state, "classify")
    assert (node.on_success, node.on_error) == ("other", "errors"), "on_error left out keeps the stored choice"
    assert [(edge.id, edge.to_node) for edge in moved.updated_state.edges] == [("e_classify_out", "other")], "the mirror edge follows"


# ── T3 / T3b: refusals ──────────────────────────────────────────────────────

# One value per non-routing argument. The census below fails when a field is
# added to the model and not here, so a new field cannot ride a routing-only call.
_NON_ROUTING_VALUES: dict[str, Any] = {
    "plugin": "llm",
    "options": {},
    "condition": "True",
    "routes": {"true": "out"},
    "fork_to": ["a"],
    "branches": ["a"],
    "policy": "require_all",
    "merge": "union",
    "trigger": None,
    "output_mode": "transform",
    "expected_output_count": 1,
    "timeout_seconds": 5,
    "description": "Classify rows.",
    "scope_name": None,
    "scope_opener": None,
    "scope_policy": None,
}


def test_the_refusal_census_covers_every_non_routing_argument() -> None:
    assert set(_NON_ROUTING_VALUES) == set(_UpsertNodeArgumentsModel.model_fields) - _ROUTING_FIELDS


@pytest.mark.parametrize("field", sorted(_NON_ROUTING_VALUES))
def test_a_non_routing_field_is_refused_by_name_and_the_same_call_without_it_applies(field: str) -> None:
    state = _reviewed_shield_state()

    refused = _rewire(state, "classify", input="scraped", **{field: _NON_ROUTING_VALUES[field]})

    assert not refused.success
    assert refused.updated_state is state
    code, message = _refusal(refused)
    assert code == "routing_only_edit_invalid"
    assert f"Remove {field} from this call." in message
    control = _rewire(state, "classify", input="scraped")
    assert control.success, "the refusal is the field, not the rewire"


def test_several_fields_are_all_named() -> None:
    refused = _rewire(_reviewed_shield_state(), "classify", plugin="llm", options={"prompt_template": "x"})
    assert _refusal(refused)[1].startswith(
        "routing_only=true changes only input, on_success and on_error. Remove options, plugin from this call."
    )


@pytest.mark.parametrize("route", ["on_success", "on_error"])
def test_an_explicit_null_route_is_refused_and_names_the_stored_route(route: str) -> None:
    state = _reviewed_shield_state()
    refused = _rewire(state, "classify", **{route: None})
    assert not refused.success and refused.updated_state is state
    code, message = _refusal(refused)
    assert code == "routing_only_edit_invalid"
    assert message.startswith(f"{route}: null is not a route.")
    stored = _node(state, "classify")
    assert f"'{ {'on_success': stored.on_success, 'on_error': stored.on_error}[route] }'" in message


def test_a_string_flag_is_not_a_boolean() -> None:
    """``StrictBool``: the string ``"true"`` is an argument error, never a routing-only call or a whole-node replace."""
    with pytest.raises(ToolArgumentError, match="upsert_node arguments"):
        execute_tool(
            "upsert_node",
            {"id": "classify", "node_type": "transform", "input": "scraped", "routing_only": "true"},
            _reviewed_shield_state(),
            _mock_catalog(),
        )


def test_an_unknown_node_is_refused() -> None:
    state = _reviewed_shield_state()
    refused = execute_tool(
        "upsert_node", {"id": "ghost", "node_type": "transform", "input": "scraped", "routing_only": True}, state, _mock_catalog()
    )
    assert not refused.success and refused.updated_state is state
    code, message = _refusal(refused)
    assert code == "node_not_found"
    assert "routing_only=true edits an existing transform" in message


def test_a_gate_and_a_mislabelled_node_type_are_refused() -> None:
    state = _reviewed_shield_state().with_node(
        NodeSpec(
            id="pick",
            node_type="gate",
            plugin=None,
            input="out",
            on_success=None,
            on_error=None,
            options={},
            condition="True",
            routes={"true": "a", "false": "b"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
    )
    gate = execute_tool(
        "upsert_node", {"id": "pick", "node_type": "gate", "input": "inbound", "routing_only": True}, state, _mock_catalog()
    )
    assert _refusal(gate)[0] == "routing_only_edit_invalid" and "node 'pick' is a gate" in _refusal(gate)[1]
    mislabelled = execute_tool(
        "upsert_node", {"id": "classify", "node_type": "aggregation", "input": "scraped", "routing_only": True}, state, _mock_catalog()
    )
    assert _refusal(mislabelled)[0] == "routing_only_edit_invalid"
    assert gate.updated_state is state and mislabelled.updated_state is state


# ── T4: a file-held, approved prompt ────────────────────────────────────────


async def _rewired_approved_file_prompt(blob_env: dict[str, Any], tmp_path: Path) -> tuple[Any, CompositionState, ToolResult]:
    """An approved file-held prompt card, then a routing-only write sending its errors to a new sink."""
    service = file_prompt._service(blob_env)
    approved = await file_prompt._save_and_approve_prompt_card(
        service, blob_env, file_prompt._wire_file(blob_env, file_prompt.blob_tools._field_declaring_sink_state(tmp_path))
    )
    assert type(approved.nodes[0].options[file_prompt.HASH_KEY]) is str, "the approval stamped the hash"
    approved = approved.with_output(
        OutputSpec(
            name="errors",
            plugin="json",
            options={"path": str(tmp_path / "errors.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            on_write_failure="discard",
        )
    )
    result = file_prompt.blob_tools.execute_tool(
        "upsert_node",
        {"id": "classify", "node_type": "transform", "input": "rows", "on_error": "errors", "routing_only": True},
        approved,
        file_prompt.blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )
    assert result.success, result.to_dict()
    return service, approved, result


@pytest.mark.asyncio
async def test_a_rewire_keeps_an_approved_file_prompt_and_every_review_row_byte_identical(blob_env: dict[str, Any], tmp_path: Path) -> None:
    _service, approved, result = await _rewired_approved_file_prompt(blob_env, tmp_path)
    before = approved.nodes[0].options
    later = result.updated_state
    after = later.nodes[0].options
    assert later.nodes[0].on_error == "errors"
    assert json.dumps(deep_thaw(after["prompt_template"])) == json.dumps(deep_thaw(before["prompt_template"])), "the file marker"
    assert _authored(after) == _authored(before), "every authored option, in stored order"
    assert _review_rows(after) == _review_rows(before), "every review row, the approved prompt card included"
    assert file_prompt._prompt_reviews(later) == ["resolved"]
    # The server-derived hash follows the rule every tool write follows: re-derived, None beside a file marker.
    assert file_prompt.HASH_KEY in after and after[file_prompt.HASH_KEY] is None
    assert after[file_prompt.HASH_KEY] == approved_prompt_artifact_hash_from_options(after)
    assert _data(result)["reopened_reviews"] == [] and _data(result)["reopened_review_count"] == 0
    assert file_prompt._verdict(later.validate())["errors"] == []

    # A second routing-only write puts the route back; the guarantee helper reads
    # a one-output graph, so it runs on that state. Two writes, still byte-identical.
    back = file_prompt.blob_tools.execute_tool(
        "upsert_node",
        {"id": "classify", "node_type": "transform", "input": "rows", "on_error": "discard", "routing_only": True},
        later,
        file_prompt.blob_tools._catalog(),
    )
    assert back.success, back.to_dict()
    restored = replace(back.updated_state, outputs=back.updated_state.outputs[:1])
    assert _authored(restored.nodes[0].options) == _authored(before)
    assert _review_rows(restored.nodes[0].options) == _review_rows(before)
    file_prompt._assert_file_prompt_holds_true_guarantees(restored, stage="after two routing-only rewires")


@pytest.mark.asyncio
async def test_a_rewired_approved_file_prompt_survives_a_save_and_reload_through_the_session_service(
    blob_env: dict[str, Any], tmp_path: Path
) -> None:
    service, approved, result = await _rewired_approved_file_prompt(blob_env, tmp_path)
    stored = approved.nodes[0]

    saved = await file_prompt._save_state(service, blob_env, result.updated_state)
    current = await service.get_current_state(UUID(blob_env["session_id"]))
    assert current is not None and current.id == saved.id, "the rewired state is the session's current saved state"
    reloaded = state_from_record(current)

    node = reloaded.nodes[0]
    after = node.options
    assert _shape(node) == (stored.plugin, stored.input, stored.on_success, "errors", stored.description)
    assert json.dumps(deep_thaw(after["prompt_template"])) == json.dumps(deep_thaw(stored.options["prompt_template"])), "the file marker"
    assert _authored(after) == _authored(stored.options), "every authored option, in stored order"
    assert _review_rows(after) == _review_rows(stored.options), "every review row, the approved prompt card included"
    assert file_prompt._prompt_reviews(reloaded) == ["resolved"]
    # The pinned rule survives the round-trip: re-derived, None beside a file marker.
    assert file_prompt.HASH_KEY in after and after[file_prompt.HASH_KEY] is None
    assert after[file_prompt.HASH_KEY] == approved_prompt_artifact_hash_from_options(after)
    assert _pending(reloaded) == _pending(approved), "the save opened no review"


def test_an_inline_approved_prompt_keeps_its_hash_under_the_same_rule() -> None:
    state = _with_sinks(_approved_state(), "errors")
    before = state.nodes[0].options

    result = execute_tool(
        "upsert_node",
        {"id": "code_themes", "node_type": "transform", "input": "source_out", "on_error": "errors", "routing_only": True},
        state,
        _mock_catalog(),
    )

    assert result.success, [entry.to_dict() for entry in result.validation.errors]
    after = result.updated_state.nodes[0].options
    assert _authored(after) == _authored(before)
    assert _review_rows(after) == _review_rows(before)
    assert (
        after["approved_prompt_artifact_hash"]
        == approved_prompt_artifact_hash_from_options(after)
        == before["approved_prompt_artifact_hash"]
    )


# ── T5: reviews the rewire re-opens ─────────────────────────────────────────


def test_a_route_out_of_the_step_leaves_the_shield_review_resolved() -> None:
    result = _rewire(_reviewed_shield_state(), "classify", on_success="elsewhere")
    assert result.success
    assert _shield_status(result.updated_state, "classify") == "resolved"
    assert _data(result)["reopened_reviews"] == [] and _data(result)["reopened_review_count"] == 0


def test_a_new_feed_reopens_the_shield_review_and_the_run_waits_on_it() -> None:
    state = _reviewed_shield_state()
    assert _shield_status(state, "classify") == "resolved"

    result = _rewire(state, "classify", input="scraped")

    assert _shield_status(result.updated_state, "classify") == "pending"
    pending = [(site.component_id, site.user_term) for site in pending_execution_interpretation_sites(result.updated_state)]
    assert ("classify", PROMPT_SHIELD_USER_TERM) in pending
    assert [row["component_id"] for row in _data(result)["reopened_reviews"]] == ["classify"]


def test_rewiring_an_upstream_step_names_the_review_it_reopens_on_another_node() -> None:
    state = _reviewed_shield_state().with_node(_web_scrape("scrape_b", input_stream="rows_b", on_success="scraped_b"))
    llm = _node(state, "classify")
    options = deep_thaw(llm.options)
    for row in options[INTERPRETATION_REQUIREMENTS_KEY]:
        if row["user_term"] == PROMPT_SHIELD_USER_TERM:
            row["accepted_artifact_hash"] = pipeline_decision_artifact_hash(llm, state.nodes, user_term=PROMPT_SHIELD_USER_TERM)
    state = state.with_node(replace(llm, options=options))
    assert _shield_status(state, "classify") == "resolved"

    result = _rewire(state, "mid", input="scraped_b")

    assert result.success, [entry.to_dict() for entry in result.validation.errors]
    assert _data(result)["routing_update"]["id"] == "mid"
    assert _data(result)["reopened_reviews"] == [
        {"component_id": "classify", "kind": "pipeline_decision", "user_term": PROMPT_SHIELD_USER_TERM}
    ]


def test_a_repeat_of_the_stored_routes_changes_nothing() -> None:
    state = _reviewed_shield_state()
    result = _rewire(state, "classify", on_success="out", on_error="stop")
    assert result.success and result.updated_state is state
    assert _data(result)["reopened_review_count"] == 0
    assert "applied_component" not in _wire(result)


def test_the_listed_reviews_are_capped_and_the_count_is_always_the_full_count() -> None:
    node = _node(_reviewed_shield_state(), "classify")
    sites = [
        InterpretationReviewSite(
            component_id=f"llm_{index:02d}",
            component_type="transform",
            user_term=PROMPT_SHIELD_USER_TERM,
            kind=InterpretationKind.PIPELINE_DECISION,
        )
        for index in range(40)
    ]
    payload = transforms._routing_update_payload(node, sites)
    assert len(payload["reopened_reviews"]) == 32
    assert payload["reopened_review_count"] == 40
    assert [row["component_id"] for row in payload["reopened_reviews"]] == [f"llm_{index:02d}" for index in range(32)]


# ── T6 / T9: the fork repair over shielded LLM steps, and the echo trap ─────


def _shielded_chain(consumers: int, *, prompt_rows: int) -> CompositionState:
    """web_scrape -> ``consumers`` LLM steps all reading its output, each with a resolved prompt-shield review."""
    chain = fork_graph.chain(steps=consumers, shared=consumers, llm_count=consumers, prompt_rows=prompt_rows)
    source = chain.sources["source"]
    state = chain.with_source(replace(source, on_success="pages")).with_node(
        _web_scrape("scrape", input_stream="pages", on_success=fork_graph.SHARED)
    )
    for index in range(consumers):
        llm = _node(state, fork_graph.step_id(index, llm_count=consumers))
        options = deep_thaw(llm.options)
        options[INTERPRETATION_REQUIREMENTS_KEY] = [
            {
                "id": f"prompt_injection_shield_review:{llm.id}",
                "kind": "pipeline_decision",
                "user_term": PROMPT_SHIELD_USER_TERM,
                "status": "resolved",
                "draft": "Public web content flows into the LLM.",
                "event_id": f"evt-shield-{llm.id}",
                "accepted_value": "Public web content flows into the LLM.",
                "accepted_artifact_hash": pipeline_decision_artifact_hash(llm, state.nodes, user_term=PROMPT_SHIELD_USER_TERM),
                "resolved_prompt_template_hash": None,
            }
        ]
        state = state.with_node(replace(llm, options=options))
    return state


def _fork_repair(state: CompositionState, consumers: int, *, routing_only: bool) -> tuple[CompositionState, list[ToolResult]]:
    ids = [fork_graph.step_id(index, llm_count=consumers) for index in range(consumers)]
    fan = execute_tool(
        "upsert_node",
        {
            "id": "fan",
            "node_type": "gate",
            "input": fork_graph.SHARED,
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": [f"br_{node_id}" for node_id in ids],
        },
        state,
        _mock_catalog(),
    )
    assert fan.success, [entry.to_dict() for entry in fan.validation.errors]
    state = fan.updated_state
    results = []
    for node_id in ids:
        if routing_only:
            # Through the JSON wire, as the provider sends it.
            arguments = json.loads(json.dumps({"id": node_id, "node_type": "transform", "input": f"br_{node_id}", "routing_only": True}))
        else:
            arguments = {**_serialize_set_pipeline_node(_node(state, node_id)), "input": f"br_{node_id}"}
        result = execute_tool("upsert_node", arguments, state, _mock_catalog())
        assert result.success, [entry.to_dict() for entry in result.validation.errors]
        state = result.updated_state
        results.append(result)
    assert "duplicate_connection_consumer" not in {entry.error_code for entry in state.validate().errors}
    return state, results


def _bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


@pytest.mark.parametrize("consumers", [2, 12])
def test_a_fork_repair_over_shielded_steps_reopens_one_named_review_per_step_and_keeps_every_option(consumers: int) -> None:
    start = _shielded_chain(consumers, prompt_rows=150)
    ids = [fork_graph.step_id(index, llm_count=consumers) for index in range(consumers)]
    assert {_shield_status(start, node_id) for node_id in ids} == {"resolved"}

    end, results = _fork_repair(start, consumers, routing_only=True)

    for node_id, result in zip(ids, results, strict=True):
        data = _data(result)
        assert data["reopened_reviews"] == [{"component_id": node_id, "kind": "pipeline_decision", "user_term": PROMPT_SHIELD_USER_TERM}]
        assert data["reopened_review_count"] == 1
        assert _bytes(data) <= 400, (node_id, _bytes(data))
        assert "applied_component" not in _wire(result)
    for node_id in ids:
        assert _authored(_node(end, node_id).options) == _authored(_node(start, node_id).options)
        assert _shield_status(end, node_id) == "pending"
    pending = {(site.component_id, site.user_term) for site in pending_execution_interpretation_sites(end)}
    assert {(node_id, PROMPT_SHIELD_USER_TERM) for node_id in ids} <= pending


@pytest.mark.parametrize("consumers", [2, 12])
def test_the_routing_only_result_does_not_grow_with_the_steps_it_keeps(consumers: int) -> None:
    _, long_prompts = _fork_repair(_shielded_chain(consumers, prompt_rows=150), consumers, routing_only=True)
    _, short_prompts = _fork_repair(_shielded_chain(consumers, prompt_rows=5), consumers, routing_only=True)
    assert [_bytes(result.to_dict()) for result in long_prompts] == [_bytes(result.to_dict()) for result in short_prompts], (
        "nothing in the result restates a step's options; what remains is the whole-graph validation"
    )


def test_a_prompt_the_whole_step_echo_carries_never_reaches_the_routing_only_result() -> None:
    # Control: a 5.7 KB prompt fits the whole-step echo (the prompt review row repeats
    # the text, so a 12 KB prompt already overflows the 16 KiB echo cap and drops it).
    marker = "[049] Criterion 0: read the record"
    _, whole = _fork_repair(_shielded_chain(2, prompt_rows=50), 2, routing_only=False)
    assert marker in json.dumps(whole[0].to_dict()), "control: the whole-step echo carries the prompt"
    _, rewired = _fork_repair(_shielded_chain(2, prompt_rows=50), 2, routing_only=True)
    assert all(marker not in json.dumps(result.to_dict()) and "applied_component" not in _wire(result) for result in rewired)


def test_a_twelve_kilobyte_prompt_never_reaches_the_routing_only_result() -> None:
    marker = "[109] Criterion 0: read the record"
    prompt = _shielded_chain(2, prompt_rows=110).nodes[0].options["prompt_template"]
    assert 12 * 1024 <= len(prompt.encode()) < 16 * 1024 and marker in prompt
    _, rewired = _fork_repair(_shielded_chain(2, prompt_rows=110), 2, routing_only=True)
    assert all(marker not in json.dumps(result.to_dict()) and "applied_component" not in _wire(result) for result in rewired)
    assert all(_bytes(_data(result)) <= 400 for result in rewired)


def test_on_a_step_above_the_echo_cap_the_receipt_is_still_the_whole_report() -> None:
    """Shape D: one LLM step with about 530 KB of options; the echo would be dropped whole, the receipt is not."""
    state = _shielded_chain(1, prompt_rows=4500)
    assert _bytes(deep_thaw(state.nodes[0].options)) > 500_000
    result = _rewire(state, "llm_00", input="pages")
    assert result.success, [entry.to_dict() for entry in result.validation.errors]
    assert "applied_component" not in _wire(result)
    assert _bytes(_data(result)) <= 400
    assert _authored(_node(result.updated_state, "llm_00").options) == _authored(_node(state, "llm_00").options)


# ── T7: the S1 repair over auto-wired shields (either S1 ruling is measured) ─


def _s1_state(tmp_path: Path) -> tuple[CompositionState, PolicyCatalogView, PluginAvailabilitySnapshot]:
    (tmp_path / "outputs").mkdir()
    view, snapshot = _guardrail_profile_view(tmp_path)
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="s1"), version=1).with_source(
        SourceSpec(
            plugin="csv",
            on_success="rows",
            on_validation_failure="discard",
            options={
                "path": "/data/in.csv",
                "schema": {"mode": "flexible", "fields": ["ticket_id: str", "body: str"], "guaranteed_fields": ["ticket_id", "body"]},
            },
        )
    )

    def node(node_id: str, **fields: Any) -> NodeSpec:
        defaults: dict[str, Any] = {"condition": None, "routes": None, "fork_to": None, "branches": None, "policy": None, "merge": None}
        return NodeSpec(id=node_id, node_type="transform", on_error="discard", **{**defaults, **fields})

    state = state.with_node(
        node("prep", plugin="passthrough", input="rows", on_success="prepped", options={"schema": {"mode": "observed"}})
    )
    for llm_id in ("llm_1", "llm_2", "llm_3"):
        options = {
            "profile": "sonnet",
            "system_prompt": "You assess.",
            "prompt_template": "Assess {{ row.ticket_id }}: {{ row.body }}.",
            "required_input_fields": ["ticket_id", "body"],
            "response_field": f"a_{llm_id}",
            "schema": {"mode": "observed"},
        }
        # The open prompt card an upsert of an LLM step stages, so the user can approve it as the card does.
        carded = _options_with_default_prompt_template_review(node_id=llm_id, plugin="llm", options=options)
        state = state.with_node(node(llm_id, plugin="llm", input="prepped", on_success=f"out_{llm_id}", options=carded))
        state = state.with_output(
            OutputSpec(name=f"out_{llm_id}", plugin="csv", options={"path": f"outputs/{llm_id}.csv"}, on_write_failure="discard")
        )
    state = wire_required_controls_state(state, snapshot, view)
    for wired in list(state.nodes):
        options = deep_thaw(wired.options)
        rows = [
            row for row in options.get(INTERPRETATION_REQUIREMENTS_KEY, []) if row["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
        ]
        for row in rows:
            artifact_hash = pipeline_decision_artifact_hash(wired, state.nodes, user_term=REQUIRED_CONTROL_AUTO_WIRED_USER_TERM)
            row.update(
                {
                    "status": "resolved",
                    "event_id": f"ack-{wired.id}",
                    "accepted_value": row["draft"],
                    "accepted_artifact_hash": artifact_hash,
                }
            )
        if rows:
            state = state.with_node(replace(wired, options=options))
    return state, view, snapshot


def _disclosures(state: CompositionState) -> dict[str, str]:
    return {
        node.id: row["status"]
        for node in state.nodes
        for row in deep_thaw(node.options).get(INTERPRETATION_REQUIREMENTS_KEY, [])
        if row["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
    }


def _s1_fork_repair(
    state: CompositionState, view: PolicyCatalogView, snapshot: PluginAvailabilitySnapshot, *, routing_only: bool
) -> tuple[CompositionState, list[str]]:
    """Put a fork gate in front of the S1 prompt shields and re-point each shield at its branch."""
    shields = sorted(node.id for node in state.nodes if node.plugin == "aws_bedrock_prompt_shield")
    fan = production_execute_tool(
        "upsert_node",
        {
            "id": "fan",
            "node_type": "gate",
            "input": _node(state, shields[0]).input,
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": [f"br_{shield}" for shield in shields],
        },
        state,
        view,
        plugin_snapshot=snapshot,
    )
    assert fan.success
    state = fan.updated_state
    for shield in shields:
        if routing_only:
            arguments: dict[str, Any] = {"id": shield, "node_type": "transform", "input": f"br_{shield}", "routing_only": True}
        else:
            arguments = {**_serialize_set_pipeline_node(_node(state, shield)), "input": f"br_{shield}"}
        result = production_execute_tool("upsert_node", arguments, state, view, plugin_snapshot=snapshot)
        assert result.success, [entry.to_dict() for entry in result.validation.errors]
        if routing_only:
            assert _data(result)["reopened_reviews"] == [
                {"component_id": shield, "kind": "pipeline_decision", "user_term": REQUIRED_CONTROL_AUTO_WIRED_USER_TERM}
            ]
        state = result.updated_state
    return state, shields


@pytest.mark.parametrize("routing_only", [True, False], ids=["routing_only", "whole_step_control"])
def test_s1_fork_repair_reopens_each_repointed_disclosure_and_ends_valid(tmp_path: Path, routing_only: bool) -> None:
    state, view, snapshot = _s1_state(tmp_path)
    assert set(_disclosures(state).values()) == {"resolved"} and len(_disclosures(state)) == 6
    state, shields = _s1_fork_repair(state, view, snapshot, routing_only=routing_only)

    assert _disclosures(state) == {node_id: ("pending" if node_id in shields else "resolved") for node_id in _disclosures(state)}, (
        "each re-pointed shield's disclosure re-opens; the content-safety controls downstream keep theirs"
    )
    assert state.validate().errors == (), [entry.to_dict() for entry in state.validate().errors]


@pytest.fixture
def operation_store(tmp_path: Path) -> Any:
    yield from _operation_store_fixture.__wrapped__(tmp_path)


def _pending(state: CompositionState) -> list[tuple[str, str, str]]:
    return sorted((site.component_id, site.kind.value, site.user_term) for site in pending_execution_interpretation_sites(state))


def _errors(state: CompositionState) -> list[tuple[str | None, str]]:
    return sorted(((entry.error_code, entry.message) for entry in state.validate().errors), key=repr)


def _shape(node: NodeSpec) -> tuple[str | None, str, str | None, str | None, str | None]:
    return (node.plugin, node.input, node.on_success, node.on_error, node.description)


async def _approve_through_service(
    operation_store: Any, state: CompositionState, cards: Sequence[tuple[str, str, str]]
) -> CompositionState:
    """Save ``state`` and approve each ``(node_id, kind, user_term)`` card as the user does; return the saved result.

    The sessions service opens the pending event and resolves it (the route the card posts to calls the same
    two methods); no review row is written by the test.
    """
    engine, repository, _authority, service, session_id = operation_store
    state_id = _checkpoint(engine, session_id, state=state)
    running, _admission = await begin(operation_store, state_id)
    try:
        for node_id, kind_value, user_term in cards:
            current = await service.get_current_state(session_id)
            assert current is not None
            options = deep_thaw(_node(state_from_record(current), node_id).options)
            kind = InterpretationKind(kind_value)
            if kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
                draft = prompt_review_draft_from_options(options)
            else:
                draft = next(row["draft"] for row in options[INTERPRETATION_REQUIREMENTS_KEY] if row["user_term"] == user_term)
            assert draft is not None
            event = await service.create_pending_interpretation_event(
                session_id=session_id,
                composition_state_id=current.id,
                affected_node_id=node_id,
                tool_call_id=f"reapprove-{node_id}",
                user_term=user_term,
                kind=kind,
                llm_draft=draft,
                model_identifier="test/model",
                model_version="test",
                provider="test",
                composer_skill_hash="a" * 64,
                session_operation_context=running.session_operation_context,
            )
            resolved, _record = await service.resolve_interpretation_event(
                session_id=session_id,
                event_id=event.id,
                choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
                amended_value=None,
                actor="user:reviewer",
                session_operation_context=running.session_operation_context,
                **await reviewed_anchor_kwargs(service, session_id, event.id, kind),
            )
            assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
        final_record = await service.get_current_state(session_id)
    finally:
        repository.release(running.session_operation_context)
    assert final_record is not None
    return state_from_record(final_record)


@pytest.mark.asyncio
async def test_s1_each_reopened_disclosure_is_approved_through_the_session_service_and_the_run_gate_clears(
    tmp_path: Path, operation_store: Any
) -> None:
    state, view, snapshot = _s1_state(tmp_path)
    prompt_cards = _pending(state)
    assert [kind for _, kind, _ in prompt_cards] == ["llm_prompt_template"] * 3, "S1 starts with only its three prompt cards open"
    state, shields = _s1_fork_repair(state, view, snapshot, routing_only=True)
    reopened = [(shield, "pipeline_decision", REQUIRED_CONTROL_AUTO_WIRED_USER_TERM) for shield in shields]
    assert _pending(state) == sorted(prompt_cards + reopened), "the rewire re-opened exactly the named disclosures"

    final = await _approve_through_service(operation_store, state, (*reopened, *prompt_cards))

    assert set(_disclosures(final).values()) == {"resolved"} and len(_disclosures(final)) == 6
    assert pending_execution_interpretation_sites(final) == (), _pending(final)
    assert final.validate().errors == (), [entry.to_dict() for entry in final.validate().errors]
    for shield in shields:
        assert _node(final, shield).input == f"br_{shield}", "approval kept the rewire"


@pytest.mark.asyncio
async def test_t5_the_reopened_shield_review_is_approved_through_the_session_service_and_the_run_gate_clears(
    operation_store: Any,
) -> None:
    state = _reviewed_shield_state()
    assert _pending(state) == [] and _shield_status(state, "classify") == "resolved"
    result = _rewire(state, "classify", input="scraped")
    shield = ("classify", "pipeline_decision", PROMPT_SHIELD_USER_TERM)
    assert _data(result)["reopened_reviews"] == [dict(zip(("component_id", "kind", "user_term"), shield, strict=True))]
    rewired = result.updated_state
    assert _pending(rewired) == [shield]

    final = await _approve_through_service(operation_store, rewired, [shield])

    assert _shield_status(final, "classify") == "resolved"
    assert pending_execution_interpretation_sites(final) == (), _pending(final)
    assert _node(final, "classify").input == "scraped", "approval kept the rewire"
    assert _authored(_node(final, "classify").options) == _authored(_node(state, "classify").options)
    # This fixture is a fragment (no source, no sinks), so it never validates clean; the S1 test above
    # carries the 0-error assertion. Here the approval adds no error to what the rewire left.
    assert _errors(final) == _errors(rewired)


# ── T5c / T5d: a dangling input, and putting a route back ──────────────────


def test_t5c_an_input_nothing_produces_is_applied_and_the_whole_graph_validation_names_it() -> None:
    state = _reviewed_shield_state()
    before = _node(state, "classify")

    result = _rewire(state, "classify", input="nowhere")

    assert result.success
    after = _node(result.updated_state, "classify")
    assert after.input == "nowhere" and after.plugin == before.plugin and _authored(after.options) == _authored(before.options)
    assert "node_input_not_reachable" in [entry.error_code for entry in result.validation.errors if "'nowhere'" in entry.message]
    assert _data(result)["reopened_reviews"] == [
        {"component_id": "classify", "kind": "pipeline_decision", "user_term": PROMPT_SHIELD_USER_TERM}
    ]


def test_t5d_putting_the_input_back_does_not_restore_the_approval_the_rewire_reopened() -> None:
    state = _reviewed_shield_state()
    original_input = _node(state, "classify").input
    away = _rewire(state, "classify", input="scraped")
    assert _data(away)["reopened_review_count"] == 1

    back = _rewire(away.updated_state, "classify", input=original_input)

    assert back.success
    restored = _node(back.updated_state, "classify")
    assert restored.input == original_input
    assert _authored(restored.options) == _authored(_node(state, "classify").options)
    # The review re-opened on the first call stays open: the run gate waits on it until the user approves it
    # again. The second call re-opened nothing new, so its receipt lists nothing.
    assert _shield_status(back.updated_state, "classify") == "pending"
    assert ("classify", "pipeline_decision", PROMPT_SHIELD_USER_TERM) in _pending(back.updated_state)
    assert _data(back)["reopened_reviews"] == [] and _data(back)["reopened_review_count"] == 0


# ── R3: a staged rewire accepted through the proposal route ─────────────────


async def _stage_proposal(
    composer: ComposerServiceImpl, sessions: Any, session_id: UUID, state: CompositionState, state_id: UUID, arguments: dict[str, Any]
) -> Any:
    """Run one ``upsert_node`` call through the batch dispatcher under explicit approval; return its pending proposal."""
    snapshot, policy_catalog = composer._policy_context.build(None)
    async with acquire_compose_context(sessions, session_id) as operation_context:
        outcome, _ = await composer._dispatch_tool_batch(
            call_model=_CallModelOutcome(
                completion=_make_llm_response(tool_calls=[{"id": "rewire-1", "name": "upsert_node", "arguments": arguments}])
            ),
            state=state,
            last_validation=None,
            last_runtime_preflight=None,
            llm_messages=[],
            recorder=BufferingRecorder(),
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache={},
            session_id=str(session_id),
            session_operation_context=operation_context,
            user_id="alice",
            user_message_id=None,
            user_message_content="Send errors to the errors output",
            current_state_id=str(state_id),
            actor="composer-web:user:alice",
            initial_version=state.version,
            deadline=asyncio.get_running_loop().time() + 60,
            progress=None,
            session_scope="routing-only-approval-test",
            advisor_calls_used=0,
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=snapshot,
            policy_catalog=policy_catalog,
        )
    assert outcome.state is state, "under explicit approval the call is staged, not applied"
    proposals = await sessions.list_composition_proposals(session_id, status="pending")
    return next(proposal for proposal in proposals if proposal.tool_call_id == "rewire-1")


@pytest.mark.asyncio
async def test_a_staged_rewire_accepted_through_the_route_commits_the_previewed_node_with_its_options_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service = _make_app(tmp_path)
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=_make_settings(data_dir=tmp_path), sessions_service=service, session_engine=service._engine
    )

    def trained_dispatch(name: str, arguments: dict[str, Any], state: Any, _route_catalog: Any, **kwargs: Any) -> Any:
        return production_execute_tool(name, arguments, state, policy_catalog, **{**kwargs, "plugin_snapshot": snapshot})

    monkeypatch.setattr(proposal_routes, "execute_tool", trained_dispatch)
    session = await service.create_session("alice", "Routing-only approval", "local")
    await service.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="user:alice")
    document = _with_sinks(_approved_state(), "errors").to_dict()
    saved = await _save_composition_state(
        service,
        session.id,
        CompositionStateData(
            sources=document["sources"],
            nodes=document["nodes"],
            edges=document["edges"],
            outputs=document["outputs"],
            metadata_=document["metadata"],
            is_valid=False,
        ),
        provenance="session_seed",
    )
    base = state_from_record(saved)
    stored = _node(base, "code_themes")
    arguments = {"id": "code_themes", "node_type": "transform", "input": stored.input, "on_error": "errors", "routing_only": True}
    preview = production_execute_tool("upsert_node", arguments, base, policy_catalog, plugin_snapshot=snapshot)
    assert preview.success, [entry.to_dict() for entry in preview.validation.errors]

    proposal = await _stage_proposal(composer, service, session.id, base, saved.id, arguments)
    assert proposal.tool_name == "upsert_node"
    assert proposal.summary.startswith('Rewire node "code_themes"'), proposal.summary
    assert (await service.get_current_state(session.id)).id == saved.id, "nothing is committed before the accept"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post(f"/api/sessions/{session.id}/proposals/{proposal.id}/accept")
        assert accepted.status_code == 200, accepted.text

    final = await service.get_current_state(session.id)
    assert final is not None and final.id != saved.id
    committed = _node(state_from_record(final), "code_themes")
    previewed = _node(preview.updated_state, "code_themes")
    assert _shape(committed) == _shape(previewed) == (stored.plugin, stored.input, stored.on_success, "errors", stored.description)
    assert json.dumps(deep_thaw(committed.options), ensure_ascii=False) == json.dumps(deep_thaw(previewed.options), ensure_ascii=False)
    assert _authored(committed.options) == _authored(stored.options), "every authored option, in stored order"
    assert _review_rows(committed.options) == _review_rows(stored.options)
    assert (await service.list_composition_proposals(session.id, status="pending")) == []


# ── T10 to T12: approval card, plain-upsert refusal, batch prerequisites ─────


def test_the_approval_card_names_the_rewire_not_a_replacement() -> None:
    arguments = {"id": "classify", "node_type": "transform", "input": "scraped", "on_error": "errors", "routing_only": True}
    summary = build_tool_proposal_summary(tool_name="upsert_node", arguments=arguments, redacted_arguments=arguments)
    assert summary.summary == 'Rewire node "classify": input "scraped", on_error "errors".'
    whole = {**arguments, "routing_only": False, "plugin": "llm", "options": {}}
    assert "Rewire" not in build_tool_proposal_summary(tool_name="upsert_node", arguments=whole, redacted_arguments=whole).summary


def test_a_plain_upsert_without_options_points_at_routing_only() -> None:
    state = _reviewed_shield_state()
    refused = execute_tool("upsert_node", {"id": "classify", "node_type": "transform", "input": "scraped"}, state, _mock_catalog())
    assert not refused.success and refused.updated_state is state
    assert "send routing_only=true with id, node_type, input and the routes that change" in _refusal(refused)[1]


def test_a_routing_only_call_depends_on_its_node() -> None:
    arguments = {"id": "late", "node_type": "transform", "input": "c", "routing_only": True}
    assert ("node", "late") in batch_dependencies.referenced_resources("upsert_node", arguments)
    assert ("node", "late") not in batch_dependencies.referenced_resources("upsert_node", {**arguments, "routing_only": False})
    state = _reviewed_shield_state()
    assert batch_dependencies.has_unavailable_prerequisite("upsert_node", arguments, {("node", "late")}, state)
    assert not batch_dependencies.has_unavailable_prerequisite(
        "upsert_node", {**arguments, "id": "classify"}, {("node", "classify")}, state
    )


# ── T13: the teaching texts ─────────────────────────────────────────────────

_REOPEN_CLAUSE = "re-opens when the rewire changes that fact"


def test_every_teaching_surface_says_a_rewire_can_reopen_a_review_and_none_says_reviews_are_kept() -> None:
    upsert = next(tool for tool in get_tool_definitions() if tool["name"] == "upsert_node")
    composer = " ".join(load_skill("pipeline_composer").split())
    capabilities = " ".join(load_skill("pipeline_capabilities").split())
    for text in (upsert["description"], composer):
        assert "(prompt shield)" in text and "of a gate's routes" in text and _REOPEN_CLAUSE in text
    assert "routing_only" in upsert["parameters"]["properties"]
    for key in ("routing_update", "options_preserved", "reopened_reviews", "reopened_review_count", "component_id", "user_term"):
        assert f"`{key}`" in upsert["description"], key
    assert "routing_only=true" in composer and "routing_only=true" in capabilities
    sources = [path.read_text(encoding="utf-8") for path in (Path(skills_package.__file__).parent).glob("*.md")]
    sources += [path.read_text(encoding="utf-8") for path in (Path(tools_package.__file__).parent).glob("*.py")]
    assert not [text for text in sources if "even for routing-only edits" in " ".join(text.split())]
    for text in (upsert["description"], json.dumps(upsert["parameters"]), composer, capabilities, _DUPLICATE_CONSUMER_REASON):
        lowered = text.lower()
        for promise in ("reviews stay", "reviews are kept", "keeps its reviews", "reviews unchanged", "leaves reviews"):
            assert promise not in lowered, promise


def test_no_text_offers_routing_only_to_an_aggregation() -> None:
    for raw in load_skill("pipeline_composer").split("\n\n"):
        paragraph = " ".join(raw.split())
        if "routing_only" in paragraph and "aggregation" in paragraph:
            assert "aggregation through `input`" in paragraph and "re-sent whole" in paragraph, paragraph
    assert "keeps a transform's options" in _DUPLICATE_CONSUMER_REASON
    assert "upsert_node" not in _DUPLICATE_CONSUMER_REASON, "the facts name no call (form B)"


# ── R1: a re-opened review's user_term never reaches the persisted tool row ──

_R1_SENTINEL = "zq-sentinel-phrase-routing-r1"


def _receipt_with_vague_term_site(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    """A real routing-only call whose receipt names one re-opened vague_term review carrying the sentinel."""
    state = _reviewed_shield_state()
    real_sites = transforms.pending_execution_interpretation_sites
    sentinel_site = InterpretationReviewSite(
        component_id="classify", component_type="transform", user_term=_R1_SENTINEL, kind=InterpretationKind.VAGUE_TERM
    )

    def with_sentinel(candidate: CompositionState) -> tuple[InterpretationReviewSite, ...]:
        sites = tuple(real_sites(candidate))
        return sites if candidate is state else (*sites, sentinel_site)

    monkeypatch.setattr(transforms, "pending_execution_interpretation_sites", with_sentinel)
    arguments = {"id": "classify", "node_type": "transform", "input": "scraped", "routing_only": True}
    result = execute_tool("upsert_node", arguments, state, _mock_catalog())
    assert result.success, [entry.to_dict() for entry in result.validation.errors]
    response = _wire(result)
    assert {"component_id": "classify", "kind": "vague_term", "user_term": _R1_SENTINEL} in response["data"]["reopened_reviews"]
    return arguments, response


def test_the_persisted_tool_row_never_carries_a_reopened_reviews_user_term(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments, response = _receipt_with_vague_term_site(monkeypatch)
    assert _R1_SENTINEL in json.dumps(response), "control: the model-facing receipt carries the term"

    redacted = redact_tool_call_response("upsert_node", response, telemetry=NoopRedactionTelemetry())
    assert _R1_SENTINEL not in json.dumps(redacted, default=str)

    # The chat-storage projection (chat_messages.tool_calls) built from the same row.
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(response)
    invocation = ComposerToolInvocation(
        tool_call_id="call-r1",
        tool_name="upsert_node",
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode("utf-8")).hexdigest(),
        result_canonical=result_canonical,
        result_hash=hashlib.sha256(result_canonical.encode("utf-8")).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=1,
        version_after=2,
        started_at=datetime(2026, 11, 21, tzinfo=UTC),
        finished_at=datetime(2026, 11, 21, tzinfo=UTC),
        latency_ms=3,
        actor="composer-web:user-test",
    )
    content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    assert _R1_SENTINEL not in content
    assert _R1_SENTINEL not in json.dumps(envelope, default=str)

    # What the row keeps: the envelope framing, and the receipt's shape with one entry per listed review.
    assert redacted["success"] is True and redacted["version"] == response["version"]
    assert len(redacted["affected_nodes"]) == len(response["affected_nodes"])
    data = redacted["data"]
    assert len(data) == 4
    listed = [value for value in data.values() if isinstance(value, list)]
    assert len(listed) == 1 and len(listed[0]) == len(response["data"]["reopened_reviews"])
    assert all(len(entry) == 3 for entry in listed[0])
