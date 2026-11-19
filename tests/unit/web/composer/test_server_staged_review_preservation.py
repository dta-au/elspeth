"""Server-staged node reviews survive authoring round trips and state writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.pipeline_proposal import AbsentBase
from elspeth.web.composer.required_controls import wire_required_controls_state
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tool_batch import _finalize_complete_set_pipeline_candidate, _finalize_completed_incremental_mutation
from elspeth.web.composer.tools import ToolContext, ToolResult
from elspeth.web.composer.tools._common import _resolver_owned_interpretation_requirement_error, _server_staged_requirement_boundary
from elspeth.web.composer.tools._dispatch import _ALL_MUTATION_TOOL_NAMES, _enforce_composition_interpretation_gate
from elspeth.web.composer.tools.declarations import EffectDomain, ToolEffects
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import (
    COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS,
    INTERPRETATION_REQUIREMENTS_KEY,
    REGISTERED_PIPELINE_DECISION_USER_TERMS,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    ServerStagedRequiredControlUserTerm,
    pending_execution_interpretation_sites,
    pipeline_decision_artifact_hash,
    project_planner_context_interpretation_requirement,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
    reconcile_authoritative_reviews,
    server_staged_review_preservation_error,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.pending_interpretation import _resolve_pipeline_decision_review
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from tests.unit.web.composer._helpers import _make_settings
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean as _stub_advisor_end_gate_clean
from tests.unit.web.composer.conftest import _fake_llm_response, _FakeComposeLLM
from tests.unit.web.composer.test_planner_authoring_aids import _catalog_with_llm_source, _guardrail_profile_view
from tests.unit.web.composer.test_tools import execute_tool
from tests.unit.web.composer.test_transform_tools_review_and_guard_parity import _reviewed_gate_state
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store_fixture
from tests.unit.web.coordination.test_graph_recovery_persistence import begin, finish
from tests.unit.web.execution.test_validation import _FakeYamlGenerator
from tests.unit.web.execution.test_validation import _make_settings as _validation_settings
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state
from tests.unit.web.sessions.test_composer_recovery_security import _checkpoint, _park

TERM = REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
KEY = INTERPRETATION_REQUIREMENTS_KEY
CODE = "server_owned_review_protected"
OWNED_TERMS = tuple(sorted(REGISTERED_PIPELINE_DECISION_USER_TERMS - COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS))


@pytest.mark.parametrize("stored_rows", ["malformed", [None], [{"user_term": 1}]])
def test_stored_review_corruption_raises(stored_rows: Any) -> None:
    with pytest.raises(AuditIntegrityError):
        _server_staged_requirement_boundary({KEY: []}, stored_options={KEY: stored_rows}, node_id="guard")


@pytest.fixture
def operation_store(tmp_path: Path):
    yield from _operation_store_fixture.__wrapped__(tmp_path)


def _node(node_id: str, **fields: Any) -> NodeSpec:
    defaults: dict[str, Any] = {
        "plugin": None,
        "input": "",
        "on_success": None,
        "on_error": None,
        "options": {},
        "condition": None,
        "routes": None,
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
    }
    return NodeSpec(id=node_id, node_type="transform", **{**defaults, **fields})


def _build(tmp_path: Path, *, resolved: bool, kind: str) -> tuple[CompositionState, Any, Any, str]:
    view, snapshot = _guardrail_profile_view(tmp_path)
    state = (
        CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="review"), version=1)
        .with_source(
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
        .with_node(
            _node(
                "assess",
                plugin="llm",
                input="rows",
                on_success="assessed",
                on_error="discard",
                options={
                    "profile": "sonnet",
                    "system_prompt": "Assess tickets.",
                    "prompt_template": "Assess {{ row.ticket_id }}: {{ row.body }}.",
                    "required_input_fields": ["ticket_id", "body"] if kind == "prompt_shield" else [],
                    "response_field": "assessment",
                    "schema": {"mode": "observed"},
                },
            )
        )
        .with_output(
            OutputSpec(
                name="assessed",
                plugin="json",
                options={
                    "path": "outputs/a.json",
                    "format": "json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            )
        )
    )
    llm = next(node for node in state.nodes if node.id == "assess")
    options = deep_thaw(llm.options)
    draft = prompt_review_draft_from_options(options)
    options[KEY] = [
        {
            "id": "prompt_template_review:assess",
            "kind": "llm_prompt_template",
            "user_term": "llm_prompt_template:assess",
            "status": "resolved",
            "draft": draft,
            "event_id": "prompt-accept",
            "accepted_value": draft,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": prompt_review_anchor_hash_from_options(options),
        }
    ]
    state = state.with_node(replace(llm, options=options))
    state = wire_required_controls_state(state, snapshot, view)
    if resolved:
        for node in tuple(state.nodes):
            rows = deep_thaw(node.options).get(KEY, [])
            if not any(row["user_term"] == TERM for row in rows):
                continue
            options = deep_thaw(node.options)
            for row in options[KEY]:
                if row["user_term"] == TERM:
                    row.update(
                        status="resolved",
                        event_id=f"ack-{node.id}",
                        accepted_value=row["draft"],
                        accepted_artifact_hash=pipeline_decision_artifact_hash(node, state.nodes, user_term=TERM),
                    )
            state = state.with_node(replace(node, options=options))
    node_id = "prompt_shield_auto_1" if kind == "prompt_shield" else "content_safety_auto_1"
    return state, view, snapshot, node_id


def _row(state: CompositionState, node_id: str) -> dict[str, Any]:
    node = next(node for node in state.nodes if node.id == node_id)
    options = deep_thaw(node.options)
    assert KEY in options, (node_id, options)
    rows = [row for row in options[KEY] if row["user_term"] == TERM]
    assert rows, (node_id, options[KEY])
    return rows[0]


def _view(state: CompositionState, view: Any, snapshot: Any) -> dict[str, Any]:
    result = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, view, plugin_snapshot=snapshot)
    assert result.success
    return result.to_dict()["data"]


def _write(tool: str, args: dict[str, Any], state: CompositionState, view: Any, snapshot: Any) -> Any:
    return execute_tool(tool, args, state, view, plugin_snapshot=snapshot)


def _node_args(state: CompositionState, view: Any, snapshot: Any, node_id: str, *, omit: bool = False) -> dict[str, Any]:
    node = next(node for node in _view(state, view, snapshot)["nodes"] if node["id"] == node_id)
    if omit:
        node["options"].pop(KEY, None)
    return node


@pytest.mark.parametrize("kind", ["prompt_shield", "content_safety"])
@pytest.mark.parametrize("term", OWNED_TERMS)
def test_t1_pending_upsert_keeps_review(tmp_path: Path, kind: str, term: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind=kind)
    before = _row(state, node_id)
    assert before["user_term"] == term
    result = _write("upsert_node", _node_args(state, view, snapshot, node_id, omit=True), state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id) == before
    assert any(site.component_id == node_id for site in pending_execution_interpretation_sites(result.updated_state))


@pytest.mark.parametrize("kind", ["prompt_shield", "content_safety"])
def test_t2_resolved_upsert_keeps_review(tmp_path: Path, kind: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=True, kind=kind)
    result = _write("upsert_node", _node_args(state, view, snapshot, node_id, omit=True), state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id) == _row(state, node_id)


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize("kind", ["prompt_shield", "content_safety"])
def test_t3_set_pipeline_keeps_review(tmp_path: Path, resolved: bool, kind: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind=kind)
    args = _view(state, view, snapshot)
    next(node for node in args["nodes"] if node["id"] == node_id)["options"].pop(KEY, None)
    result = _write("set_pipeline", args, state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id) == _row(state, node_id)


@pytest.mark.parametrize("tool", ["upsert_node", "patch_node_options", "set_pipeline"])
@pytest.mark.parametrize("kind", ["prompt_shield", "content_safety"])
@pytest.mark.parametrize("term", OWNED_TERMS)
def test_t4_explicit_empty_list_refused(tmp_path: Path, kind: str, tool: str, term: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind=kind)
    assert _row(state, node_id)["user_term"] == term
    if tool == "patch_node_options":
        args = {"node_id": node_id, "patch": {KEY: []}}
    elif tool == "upsert_node":
        args = _node_args(state, view, snapshot, node_id, omit=True)
        args["options"][KEY] = []
    else:
        args = _view(state, view, snapshot)
        next(node for node in args["nodes"] if node["id"] == node_id)["options"][KEY] = []
    result = _write(tool, args, state, view, snapshot)
    assert not result.success
    assert CODE in {entry.error_code for entry in result.validation.errors}
    assert result.updated_state is state


@pytest.mark.parametrize("tool", ["upsert_node", "set_pipeline"])
def test_t5_exact_authoring_echo_succeeds(tmp_path: Path, tool: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    args = _node_args(state, view, snapshot, node_id) if tool == "upsert_node" else _view(state, view, snapshot)
    result = _write(tool, args, state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id) == _row(state, node_id)


@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize("tool", ["upsert_node", "patch_node_options", "set_pipeline"])
@pytest.mark.parametrize("echo", ["raw", "planner", "shell"])
def test_t5_stored_echo_is_kept(tmp_path: Path, resolved: bool, tool: str, echo: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    stored = next(node for node in state.nodes if node.id == node_id)
    rows = deep_thaw(stored.options)[KEY]
    if echo == "planner":
        rows = [project_planner_context_interpretation_requirement(row) if row["user_term"] == TERM else row for row in rows]
    elif echo == "shell":
        rows = [
            {key: row[key] for key in ("kind", "user_term", "draft") if key in row} if row["user_term"] == TERM else row for row in rows
        ]
    if tool == "patch_node_options":
        args = {"node_id": node_id, "patch": {KEY: rows}}
    elif tool == "upsert_node":
        args = _node_args(state, view, snapshot, node_id, omit=True)
        args["options"][KEY] = rows
    else:
        args = _view(state, view, snapshot)
        node = next(node for node in args["nodes"] if node["id"] == node_id)
        node["options"][KEY] = rows
    result = _write(tool, args, state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id) == _row(state, node_id)


def test_t4_resolved_empty_list_is_refused(tmp_path: Path) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=True, kind="content_safety")
    result = _write("patch_node_options", {"node_id": node_id, "patch": {KEY: []}}, state, view, snapshot)
    assert not result.success
    assert CODE in {error.error_code for error in result.validation.errors}


def test_t6_changed_or_unstored_row_refused(tmp_path: Path) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    args = _node_args(state, view, snapshot, node_id)
    args["options"][KEY][0]["draft"] += " changed"
    result = _write("upsert_node", args, state, view, snapshot)
    assert not result.success
    assert "interpretation_requirements_invalid" in {entry.error_code for entry in result.validation.errors}
    node = next(item for item in state.nodes if item.id == node_id)
    without_row = deep_thaw(node.options)
    del without_row[KEY]
    unstored_state = state.with_node(replace(node, options=without_row))
    echoed = _node_args(state, view, snapshot, node_id)
    unstored = _write("upsert_node", echoed, unstored_state, view, snapshot)
    assert not unstored.success
    assert "interpretation_requirements_invalid" in {entry.error_code for entry in unstored.validation.errors}


@pytest.mark.parametrize("resolved", [False, True])
def test_t7_pending_node_removal_refused(tmp_path: Path, resolved: bool) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    result = _write("remove_node", {"id": node_id}, state, view, snapshot)
    assert result.success is resolved, result.to_dict()
    if not resolved:
        assert CODE in {entry.error_code for entry in result.validation.errors}


@pytest.mark.parametrize("resolved", [False, True])
def test_t8_node_type_change_refused(tmp_path: Path, resolved: bool) -> None:
    from elspeth.web.interpretation_state import server_staged_review_preservation_error

    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    result = _write("upsert_node", {"id": node_id, "node_type": "queue", "input": node_id, "options": {}}, state, view, snapshot)
    assert not result.success
    assert CODE in {entry.error_code for entry in result.validation.errors}
    node = next(item for item in state.nodes if item.id == node_id)
    assert server_staged_review_preservation_error(state, state.with_node(replace(node, plugin="passthrough"))) is not None
    catalog = _catalog_with_llm_source()
    broader_snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    broader_view = PolicyCatalogView.for_trained_operator(catalog, broader_snapshot)
    plugin_change = _write(
        "upsert_node",
        {
            "id": node_id,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": node.input,
            "on_success": node.on_success,
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        },
        state,
        broader_view,
        broader_snapshot,
    )
    assert not plugin_change.success
    assert CODE in {entry.error_code for entry in plugin_change.validation.errors}
    if not resolved:
        args = _view(state, view, snapshot)
        renamed = next(item for item in args["nodes"] if item["id"] == node_id)
        renamed["id"] = "renamed_control"
        renamed["options"].pop(KEY, None)
        replacement = _write("set_pipeline", args, state, view, snapshot)
        assert not replacement.success
        assert CODE in {entry.error_code for entry in replacement.validation.errors}


@pytest.mark.parametrize("resolved", [False, True])
def test_same_plugin_aggregation_retype_is_refused(tmp_path: Path, resolved: bool) -> None:
    from elspeth.web.interpretation_state import server_staged_review_preservation_error

    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    node = next(item for item in state.nodes if item.id == node_id)
    retyped = state.with_node(replace(node, node_type="aggregation"))
    assert server_staged_review_preservation_error(state, retyped) is not None
    args = _node_args(state, view, snapshot, node_id, omit=True)
    args["node_type"] = "aggregation"
    result = _write("upsert_node", args, state, view, snapshot)
    assert not result.success
    assert CODE in {entry.error_code for entry in result.validation.errors}


def test_non_list_proposed_review_rows_are_integrity_failure(tmp_path: Path) -> None:
    from elspeth.web.interpretation_state import server_staged_review_preservation_error

    state, _, _, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    node = next(item for item in state.nodes if item.id == node_id)
    proposed = state.with_node(replace(node, options={**deep_thaw(node.options), KEY: "not rows"}))
    with pytest.raises(AuditIntegrityError, match="are not rows"):
        server_staged_review_preservation_error(state, proposed)


def _stored_non_owned_review(tmp_path: Path, *, kind: str, term: str, resolved: bool = False) -> tuple[CompositionState, str]:
    state, _, _, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    node = next(item for item in state.nodes if item.id == node_id)
    options = deep_thaw(node.options)
    options[KEY][0]["kind"] = kind
    options[KEY][0]["user_term"] = term
    return state.with_node(replace(node, options=options)), node_id


@pytest.mark.parametrize("kind,term", [("vague_term", TERM), ("pipeline_decision", TERM + " ")])
def test_stored_non_owned_review_is_not_preserved_carried_or_echoed(tmp_path: Path, kind: str, term: str) -> None:
    state, node_id = _stored_non_owned_review(tmp_path, kind=kind, term=term)
    node = next(item for item in state.nodes if item.id == node_id)
    options = deep_thaw(node.options)
    assert server_staged_review_preservation_error(state, state.without_node(node_id)) is None
    supplied = {KEY: [options[KEY][0]]}
    rewritten, error = _server_staged_requirement_boundary(supplied, stored_options=options, node_id=node_id)
    assert error is None
    assert rewritten == supplied
    omitted = dict(options)
    del omitted[KEY]
    proposed = state.with_node(replace(node, options=omitted))
    assert server_staged_review_preservation_error(state, proposed) is None
    reconciled = reconcile_authoritative_reviews(state, proposed)
    assert not any(row["user_term"] == term for row in deep_thaw(next(n for n in reconciled.nodes if n.id == node_id).options).get(KEY, []))


def test_stored_padded_resolved_review_survives_option_edit(tmp_path: Path) -> None:
    state, node_id = _stored_non_owned_review(tmp_path, kind="pipeline_decision", term=TERM + " ", resolved=True)
    node = next(item for item in state.nodes if item.id == node_id)
    options = deep_thaw(node.options)
    options["schema"] = {"mode": "flexible"}
    proposed = state.with_node(replace(node, options=options))
    reconciled = reconcile_authoritative_reviews(state, proposed)
    row = deep_thaw(next(n for n in reconciled.nodes if n.id == node_id).options)[KEY][0]
    assert row["status"] == "resolved"


@pytest.mark.parametrize("tool", ["upsert_node", "patch_node_options", "splice_transform", "set_pipeline"])
@pytest.mark.parametrize("kind", ["vague_term", "llm_prompt_template", "pipeline_decision"])
@pytest.mark.parametrize(
    "term",
    [
        TERM,
        TERM + " ",
        " " + TERM,
        TERM.upper(),
        "".join(chr(ord(char) + 0xFEE0) for char in TERM),
        "required_control_auto_\u200dwired",
        "required_control_auto_\u034fwired",
        "required_control_auto_\ufe0fwired",
        "required_control_auto_wi\u0301red",
        "required_control_auto_w\u0456red",
        "required_control_auto_\0wired",
        # Every letter with a Cyrillic look-alike replaced (10 of 27 positions).
        "r\u0435qu\u0456r\u0435d_\u0441\u043entr\u043el_\u0430ut\u043e_w\u0456r\u0435d",
        # Greek look-alike letters, full-width zeros, and an invisible Hangul filler.
        "requ\u03b9red_c\u03bfntr\u03bfl_\u03b1uto_wired",
        "required_c\uff10ntr\uff10l_aut\uff10_wired",
        "required_control_auto_\u115fwired",
        # A braille blank, a digit one for l, a stroked l, and upper-case Greek
        # omicrons (the look-alike map applies after case folding).
        "required_control_auto_\u2800wired",
        "required_contro1_auto_wired",
        "required_contro\u0142_auto_wired",
        "required_c\u039fntr\u039fl_aut\u039f_wired",
    ],
)
def test_model_reserved_term_is_refused_at_option_doors(tmp_path: Path, tool: str, kind: str, term: str) -> None:
    from tests.unit.web.composer.test_splice_transform_tool import _arguments as splice_arguments
    from tests.unit.web.composer.test_splice_transform_tool import _context as splice_context
    from tests.unit.web.composer.test_splice_transform_tool import _state as splice_state

    row = {"kind": kind, "user_term": term, "draft": "model authored review"}
    if tool == "splice_transform":
        state = splice_state()
        context = splice_context()
        args = splice_arguments(options={"schema": {"mode": "observed"}, KEY: [row]})
        result = _write(tool, args, state, context.catalog, context.plugin_snapshot)
    else:
        state, view, snapshot, _ = _build(tmp_path, resolved=False, kind="content_safety")
        if tool == "patch_node_options":
            args = {"node_id": "assess", "patch": {KEY: [row]}}
        elif tool == "upsert_node":
            args = _node_args(state, view, snapshot, "assess", omit=True)
            args["options"][KEY] = [row]
        else:
            args = _view(state, view, snapshot)
            next(node for node in args["nodes"] if node["id"] == "assess")["options"][KEY].append(row)
        result = _write(tool, args, state, view, snapshot)
    assert not result.success, result.to_dict()
    assert "interpretation_requirements_invalid" in {error.error_code for error in result.validation.errors}
    assert result.updated_state is state
    messages = [error.message for error in result.validation.errors]
    assert any("server-owned user_term" in message for message in messages)


@pytest.mark.parametrize(
    "term",
    [
        "tone",
        "می‌روم",
        "👩‍💻",
        "क्‍ष",
        "required_control_auto_wired_notes",
        # Same skeleton length as the reserved term, in non-Latin scripts.
        "東京都の大規模な文書処理パイプラインの品質管理担当者様",
        "Сегодняшнийпогодныйпрогнозд",
        # Mixed scripts: the reserved prefix followed by an unrelated phrase.
        "required_control_データ品質管理検査用",
        "required_control_аудит_журнал",
    ],
)
def test_unrelated_model_review_terms_pass_reserved_term_admission(term: str) -> None:
    row = {"kind": "vague_term", "user_term": term, "draft": "Review wording"}
    assert _resolver_owned_interpretation_requirement_error({KEY: [row]}, tool_name="upsert_node", component_id="assess") is None


@pytest.mark.parametrize("term", ["می‌روم", "👩‍💻", "क्‍ष"])
def test_unrelated_unicode_review_terms_can_be_staged(tmp_path: Path, term: str) -> None:
    state, view, snapshot, _ = _build(tmp_path, resolved=False, kind="content_safety")
    args = _node_args(state, view, snapshot, "assess", omit=True)
    args["options"][KEY] = [{"kind": "vague_term", "user_term": term, "draft": "Review wording"}]
    args["options"]["prompt_template_parts"] = [
        {"kind": "text", "text": "Assess "},
        {"kind": "interpretation_ref", "requirement_id": term + ":assess"},
    ]
    args["options"]["prompt_template"] = "Assess pending interpretation"
    result = _write("upsert_node", args, state, view, snapshot)
    assert result.success, result.to_dict()
    staged = deep_thaw(next(node for node in result.updated_state.nodes if node.id == "assess").options)[KEY]
    assert any(row["user_term"] == term for row in staged)


def test_only_nominal_server_term_can_be_authored() -> None:
    row = {"kind": "pipeline_decision", "user_term": ServerStagedRequiredControlUserTerm(TERM), "draft": "Review control"}
    assert _resolver_owned_interpretation_requirement_error({KEY: [row]}, tool_name="upsert_node", component_id="assess") is None
    row["user_term"] = TERM
    assert "server-owned user_term" in str(
        _resolver_owned_interpretation_requirement_error({KEY: [row]}, tool_name="upsert_node", component_id="assess")
    )


@pytest.mark.parametrize("resolved", [False, True])
def test_existing_node_splice_exact_stored_echo_is_refused(tmp_path: Path, resolved: bool) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="prompt_shield")
    state = replace(state, edges=())
    node = next(item for item in state.nodes if item.id == node_id)
    args = {
        "predecessor_id": "source",
        "successor_id": "assess",
        "node": {
            "id": node_id,
            "plugin": node.plugin,
            "on_error": node.on_error,
            "description": node.description,
            "options": deep_thaw(node.options),
        },
    }
    result = _write("splice_transform", args, state, view, snapshot)
    assert not result.success, result.to_dict()
    assert "interpretation_requirements_invalid" in {error.error_code for error in result.validation.errors}
    assert result.updated_state is state


@pytest.mark.parametrize("resolved", [False, True])
def test_t9_input_move_keeps_or_reopens(tmp_path: Path, resolved: bool) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    args = _node_args(state, view, snapshot, node_id, omit=True)
    args["input"] = "other_rows"
    result = _write("upsert_node", args, state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id)["status"] == "pending"


@pytest.mark.asyncio
async def test_t10_planner_staging_refuses_missing_review(tmp_path: Path) -> None:
    from elspeth.web.composer.pipeline_planner import _build_valid_pipeline_plan, _PipelineCandidateRejected

    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    without_review = state.without_node(node_id)
    candidate = SimpleNamespace(
        acceptable=True,
        result=ToolResult(success=True, updated_state=without_review, validation=without_review.validate(), affected_nodes=()),
        prepared_inline_blob=None,
    )

    async def run_sync(*_args: Any) -> Any:
        return candidate

    with pytest.raises(_PipelineCandidateRejected) as caught:
        await _build_valid_pipeline_plan(
            pipeline=_view(state, view, snapshot),
            current_state=state,
            base=AbsentBase(),
            repair_count=0,
            skill_hash="a" * 64,
            tool_call_id="call-review",
            terminal_context=ToolContext(catalog=view, plugin_snapshot=snapshot),
            custody_config=SimpleNamespace(),
            originating_message=SimpleNamespace(),
            run_sync=run_sync,
            model_identifier="test",
            model_version="test",
            provider="test",
        )
    assert CODE in {entry.error_code for entry in caught.value.result.validation.errors}


@pytest.mark.asyncio
async def test_t10_approval_refuses_renamed_control_before_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, composer_service_with_real_sessions: ComposerServiceImpl, result_session_id: str
) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    arguments = _view(state, view, snapshot)
    source = state.sources["source"]
    source_options = deep_thaw(source.options)
    source_path = tmp_path / "blobs" / result_session_id / "in.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("ticket_id,body\n1,example\n")
    source_options["path"] = str(source_path)
    state = state.with_source(replace(source, options=source_options))
    sessions = composer_service_with_real_sessions._require_sessions_service()
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path), sessions_service=sessions
    )
    monkeypatch.setattr(composer._policy_context, "build", lambda _user_id: (snapshot, view))
    session_uuid = UUID(result_session_id)
    data = state.to_dict()
    saved = await _save_composition_state(
        sessions,
        session_uuid,
        CompositionStateData(
            sources=data["sources"], nodes=data["nodes"], edges=data["edges"], outputs=data["outputs"], metadata_=data["metadata"]
        ),
        provenance="session_seed",
    )
    await sessions.update_composer_preferences(
        session_uuid, trust_mode="explicit_approve", density_default="high", actor="user:phase3-test-user"
    )
    arguments["source"]["options"]["path"] = str(source_path)
    renamed = next(node for node in arguments["nodes"] if node["id"] == node_id)
    renamed["id"] = "replacement_control"
    renamed["options"].pop(KEY, None)
    llm = _FakeComposeLLM(
        (
            _fake_llm_response(tool_calls=({"id": "review_edit", "name": "set_pipeline", "arguments": arguments},)),
            _fake_llm_response(content="The proposed edit was reviewed."),
        )
    )
    result = await composer._run_one_turn_for_test(
        llm=llm, session_id=result_session_id, current_state_id=str(saved.id), initial_state=state_from_record(saved)
    )
    feedback = result.tool_outcomes[0].response
    assert isinstance(feedback, ToolResult)
    assert not feedback.success
    assert CODE in {error.error_code for error in feedback.validation.errors}, [error.message for error in feedback.validation.errors]
    assert await sessions.list_composition_proposals(session_uuid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [False, True])
async def test_t11_finalizer_keeps_existing_node_review(tmp_path: Path, resolved: bool) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    args = _view(state, view, snapshot)
    removed = next(node for node in args["nodes"] if node["id"] == node_id)
    args["nodes"] = [node for node in args["nodes"] if node["id"] != node_id]
    for node in args["nodes"]:
        if node.get("on_success") == removed["input"]:
            node["on_success"] = removed["on_success"]
    context = ToolContext(catalog=view, plugin_snapshot=snapshot)
    finalized = await _finalize_complete_set_pipeline_candidate(args, state, context, plugin_snapshot=snapshot, policy_catalog=view)
    assert finalized.candidate.acceptable, finalized.candidate.result.to_dict()["validation"]["errors"]
    assert _row(finalized.candidate.result.updated_state, node_id)["status"] == ("resolved" if resolved else "pending")
    dispatched = _write("set_pipeline", dict(finalized.arguments), state, view, snapshot)
    assert dispatched.success, dispatched.to_dict()
    assert _row(dispatched.updated_state, node_id) == _row(state, node_id)


def test_t12_prompt_review_is_derived_from_llm_options(tmp_path: Path) -> None:
    state, view, snapshot, _ = _build(tmp_path, resolved=False, kind="content_safety")
    args = _node_args(state, view, snapshot, "assess", omit=True)
    result = _write("upsert_node", args, state, view, snapshot)
    assert result.success, result.to_dict()
    node = next(node for node in result.updated_state.nodes if node.id == "assess")
    prompt_row = next(row for row in deep_thaw(node.options)[KEY] if row["kind"] == "llm_prompt_template")
    previous_prompt_row = next(
        row
        for row in deep_thaw(next(node for node in state.nodes if node.id == "assess").options)[KEY]
        if row["kind"] == "llm_prompt_template"
    )
    assert prompt_row == previous_prompt_row
    changed_args = _node_args(state, view, snapshot, "assess", omit=True)
    changed_args["options"]["prompt_template"] = "Reassess {{ row.ticket_id }}: {{ row.body }}."
    changed = _write("upsert_node", changed_args, state, view, snapshot)
    assert changed.success, changed.to_dict()
    changed_node = next(node for node in changed.updated_state.nodes if node.id == "assess")
    changed_prompt = next(row for row in deep_thaw(changed_node.options)[KEY] if row["kind"] == "llm_prompt_template")
    assert changed_prompt["status"] == "pending"
    gate_state = _reviewed_gate_state()
    gate = gate_state.nodes[0]
    gate_options = deep_thaw(gate.options)
    del gate_options[KEY]
    proposed = gate_state.with_node(replace(gate, options=gate_options))
    reconciled = reconcile_authoritative_reviews(gate_state, proposed)
    assert KEY not in reconciled.nodes[0].options


def test_t13_resolved_session_review_survives_resend(tmp_path: Path) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    serialized = state.to_dict()
    record = CompositionStateRecord(
        id=uuid4(),
        session_id=uuid4(),
        version=state.version,
        nodes=serialized["nodes"],
        edges=serialized["edges"],
        outputs=serialized["outputs"],
        metadata_=serialized["metadata"],
        is_valid=True,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
        sources=serialized["sources"],
    )
    draft = _row(state, node_id)["draft"]
    _, patched_nodes, _ = _resolve_pipeline_decision_review(
        record,
        event_id="review-event",
        affected_node_id=node_id,
        user_term=TERM,
        llm_draft=draft,
        accepted_value=draft,
    )
    patched = next(node for node in patched_nodes if node["id"] == node_id)
    original = next(node for node in state.nodes if node.id == node_id)
    resolved = state.with_node(replace(original, options=patched["options"]))
    assert _row(resolved, node_id)["status"] == "resolved"
    result = _write("upsert_node", _node_args(resolved, view, snapshot, node_id, omit=True), resolved, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id) == _row(resolved, node_id)


@pytest.mark.asyncio
async def test_t13_sessions_service_resolution_survives_resend(tmp_path: Path, operation_store: Any) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    engine, repository, _authority, service, session_id = operation_store
    state_id = _checkpoint(engine, session_id, state=state)
    running, _admission = await begin(operation_store, state_id)
    try:
        draft = _row(state, node_id)["draft"]
        event = await service.create_pending_interpretation_event(
            session_id=session_id,
            composition_state_id=state_id,
            affected_node_id=node_id,
            tool_call_id="review-control",
            user_term=TERM,
            kind=InterpretationKind.PIPELINE_DECISION,
            llm_draft=draft,
            model_identifier="test/model",
            model_version="test",
            provider="test",
            composer_skill_hash="a" * 64,
            session_operation_context=running.session_operation_context,
        )
        _resolved, record = await service.resolve_interpretation_event(
            session_id=session_id,
            event_id=event.id,
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="user:reviewer",
            session_operation_context=running.session_operation_context,
        )
        resolved = state_from_record(record)
        assert _row(resolved, node_id)["status"] == "resolved"
        result = _write("upsert_node", _node_args(resolved, view, snapshot, node_id, omit=True), resolved, view, snapshot)
        assert result.success, result.to_dict()
        assert _row(result.updated_state, node_id) == _row(resolved, node_id)
    finally:
        repository.release(running.session_operation_context)


def test_t16_registered_mutations_keep_review(tmp_path: Path) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    cases = {
        "upsert_node": _node_args(state, view, snapshot, node_id, omit=True),
        "patch_node_options": {"node_id": node_id, "patch": {"schema": {"mode": "observed"}}},
        "remove_node": {"id": node_id},
        "set_pipeline": _view(state, view, snapshot),
        "set_metadata": {"patch": {"name": "review"}},
        "clear_source": {},
        "patch_output_options": {"sink_name": "assessed", "patch": {"schema": {"mode": "observed"}}},
        "patch_source_options": {"patch": {"schema": {"mode": "flexible", "fields": ["ticket_id: str", "body: str"]}}},
        "remove_edge": {"id": "absent_edge"},
        "remove_output": {"sink_name": "assessed"},
        "set_output": {
            "sink_name": "assessed",
            "plugin": "json",
            "options": {
                "path": "outputs/a.json",
                "format": "json",
                "schema": {"mode": "observed"},
                "mode": "write",
                "collision_policy": "auto_increment",
            },
        },
        "set_source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "/data/in.csv", "schema": {"mode": "flexible", "fields": ["ticket_id: str", "body: str"]}},
        },
        "upsert_edge": {"id": "edge_new", "from_node": "assess", "to_node": node_id, "edge_type": "on_success"},
    }
    skipped = {
        "change_substring": "needs an exact-text target",
        "create_blob": "needs blob custody",
        "delete_blob": "needs a blob record",
        "set_source_from_blob": "needs a blob record",
        "set_source_from_blobs": "needs blob records",
        "splice_transform": "needs a splice target",
        "update_blob": "needs a blob record",
        "wire_blob_inline_ref": "needs a blob record",
        "wire_secret_ref": "needs an authorized secret reference",
    }
    assert set(cases) | set(skipped) == _ALL_MUTATION_TOOL_NAMES
    assert not set(cases) & set(skipped)
    for tool, args in cases.items():
        result = _write(tool, args, state, view, snapshot)
        assert _row(result.updated_state, node_id)["status"] == "pending", (tool, result.to_dict())
    removed = state.without_node(node_id)
    result = _enforce_composition_interpretation_gate(
        ToolResult(success=True, updated_state=removed, validation=removed.validate(), affected_nodes=()),
        tool_name="future_metadata_writer",
        effects=ToolEffects((EffectDomain.INTERPRETATION,)),
        prior_state=state,
    )
    assert not result.success
    assert CODE in {entry.error_code for entry in result.validation.errors}


def test_t14_state_gate_refuses_changed_review(tmp_path: Path) -> None:
    from elspeth.web.interpretation_state import server_staged_review_preservation_error

    state, _, _, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    node = next(node for node in state.nodes if node.id == node_id)
    options = deep_thaw(node.options)
    options[KEY][0]["status"] = "resolved"
    changed = state.with_node(replace(node, options=options))
    assert server_staged_review_preservation_error(state, changed) is not None
    missing_options = deep_thaw(node.options)
    del missing_options[KEY]
    missing = state.with_node(replace(node, options=missing_options))
    assert server_staged_review_preservation_error(state, missing) is not None

    resolved, _, _, _ = _build(tmp_path, resolved=True, kind="content_safety")
    resolved_node = next(item for item in resolved.nodes if item.id == node_id)
    altered_options = deep_thaw(resolved_node.options)
    altered_options[KEY][0]["accepted_value"] += " revised"
    altered = resolved.with_node(replace(resolved_node, options=altered_options))
    assert server_staged_review_preservation_error(resolved, altered) is not None
    assert server_staged_review_preservation_error(resolved, resolved.without_node(node_id)) is None
    assert server_staged_review_preservation_error(state, state.without_node(node_id)) is not None
    assert server_staged_review_preservation_error(state, state.with_node(replace(node, plugin="passthrough"))) is not None


def test_t15_registry_partition() -> None:
    from elspeth.web.interpretation_state import SERVER_STAGED_PIPELINE_DECISION_USER_TERMS

    server_staged = REGISTERED_PIPELINE_DECISION_USER_TERMS - COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS
    assert server_staged == {TERM}
    assert server_staged == SERVER_STAGED_PIPELINE_DECISION_USER_TERMS
    assert not server_staged & COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS


def test_t17_option_edit_reopens_resolved_review(tmp_path: Path) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=True, kind="content_safety")
    result = _write("patch_node_options", {"node_id": node_id, "patch": {"fields": ["assessment", "body"]}}, state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id)["status"] == "pending"
    schema = _write(
        "patch_node_options",
        {"node_id": node_id, "patch": {"schema": {"mode": "observed", "guaranteed_fields": ["assessment"]}}},
        state,
        view,
        snapshot,
    )
    assert schema.success, schema.to_dict()
    assert _row(schema.updated_state, node_id)["status"] == "pending"
    assert any(site.component_id == node_id for site in pending_execution_interpretation_sites(schema.updated_state))


def test_t18_same_options_keep_resolved_review(tmp_path: Path) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=True, kind="content_safety")
    args = _node_args(state, view, snapshot, node_id, omit=True)
    result = _write("upsert_node", args, state, view, snapshot)
    assert result.success, result.to_dict()
    assert _row(result.updated_state, node_id) == _row(state, node_id)
    patch = _write("patch_node_options", {"node_id": node_id, "patch": {"fields": ["assessment"]}}, state, view, snapshot)
    assert patch.success, patch.to_dict()
    assert _row(patch.updated_state, node_id) == _row(state, node_id)


def test_t19_state_gate_checks_options(tmp_path: Path) -> None:
    from elspeth.web.interpretation_state import server_staged_review_preservation_error

    state, _, _, node_id = _build(tmp_path, resolved=True, kind="content_safety")
    node = next(node for node in state.nodes if node.id == node_id)
    options = deep_thaw(node.options)
    options["schema"] = {"mode": "flexible"}
    changed = state.with_node(replace(node, options=options))
    assert server_staged_review_preservation_error(state, changed) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [False, True])
@pytest.mark.parametrize("action", ["reset_to_turn_start", "reset_to_safe_checkpoint", "restore_parked_graph"])
async def test_recovery_restores_recorded_rows(tmp_path: Path, operation_store: Any, resolved: bool, action: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=resolved, kind="content_safety")
    engine, repository, _authority, service, session_id = operation_store
    state_id = _checkpoint(engine, session_id, state=state)
    park_id = _park(engine, session_id, state_id) if action == "restore_parked_graph" else None
    running, admission = await begin(operation_store, state_id)
    try:
        settlement = await service.prepare_graph_recovery(
            session_id,
            context=admission.context,
            intent=GraphRecoveryIntent(action, "Restore the recorded draft.", park_id, "recovery-review"),
            session_operation_context=running.session_operation_context,
        )
        assert settlement.failure_reason is None
        assert settlement.data is not None
        terminal = await finish(operation_store, running, settlement)
        assert terminal.status == "completed"
        latest = await service.get_current_state(session_id)
        assert latest is not None
        expected = next(node for node in state.to_dict()["nodes"] if node["id"] == node_id)
        actual = next(node for node in latest.nodes if node["id"] == node_id)
        assert deep_thaw(actual["options"]) == expected["options"]
        assert canonical_json(deep_thaw(actual["options"])) == canonical_json(expected["options"])
        if not resolved:
            assert _row(state_from_record(latest), node_id) == _row(state, node_id)
            assert canonical_json(_row(state_from_record(latest), node_id)) == canonical_json(_row(state, node_id))
        restored = state_from_record(latest)
        next_write = _write("upsert_node", _node_args(restored, view, snapshot, "assess", omit=True), restored, view, snapshot)
        assert next_write.success, next_write.to_dict()
        assert _row(next_write.updated_state, node_id) == _row(state, node_id)
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset_to_turn_start", "reset_to_safe_checkpoint", "restore_parked_graph"])
async def test_recovery_of_valid_graph_without_control_is_refused_and_restaged(tmp_path: Path, operation_store: Any, action: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    assess = next(node for node in state.nodes if node.id == "assess")
    no_control = state.without_node(node_id).with_node(replace(assess, on_success="assessed"))
    assert no_control.validate().is_valid
    engine, repository, _authority, service, session_id = operation_store
    state_id = _checkpoint(engine, session_id, state=no_control)
    park_id = _park(engine, session_id, state_id) if action == "restore_parked_graph" else None
    running, admission = await begin(operation_store, state_id)
    try:
        settlement = await service.prepare_graph_recovery(
            session_id,
            context=admission.context,
            intent=GraphRecoveryIntent(action, "Restore the recorded draft.", park_id, "recovery-coverage"),
            session_operation_context=running.session_operation_context,
        )
        assert settlement.data is not None
        terminal = await finish(operation_store, running, settlement)
        assert terminal.status == "completed"
        latest = await service.get_current_state(session_id)
        assert latest is not None
        recovered = state_from_record(latest)
        validation = validate_pipeline_for_trained_operator(
            recovered,
            _validation_settings(str(tmp_path)),
            _FakeYamlGenerator(),
            plugin_snapshot=snapshot,
            profile_registry=view._profiles,
            catalog=view._full,
        )
        assert not validation.is_valid
        assert "required_control_coverage" in {error.error_code for error in validation.errors}
        assert recovered.validate().is_valid
        next_write = _write("set_metadata", {"patch": {"name": "review recovered"}}, recovered, view, snapshot)
        assert next_write.success, next_write.to_dict()
        finalized = await _finalize_completed_incremental_mutation(next_write, recovered, plugin_snapshot=snapshot, policy_catalog=view)
        assert any(node.id == node_id for node in finalized.updated_state.nodes), next_write.validation.errors
        assert _row(finalized.updated_state, node_id)["status"] == "pending"
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset_to_turn_start", "reset_to_safe_checkpoint", "restore_parked_graph"])
async def test_recovery_restage_waits_for_valid_graph(tmp_path: Path, operation_store: Any, action: str) -> None:
    state, view, snapshot, node_id = _build(tmp_path, resolved=False, kind="content_safety")
    engine, repository, _authority, service, session_id = operation_store
    state_id = _checkpoint(engine, session_id, state=state.without_node(node_id))
    park_id = _park(engine, session_id, state_id) if action == "restore_parked_graph" else None
    running, admission = await begin(operation_store, state_id)
    try:
        settlement = await service.prepare_graph_recovery(
            session_id,
            context=admission.context,
            intent=GraphRecoveryIntent(action, "Restore recorded graph.", park_id, "restage-evidence"),
            session_operation_context=running.session_operation_context,
        )
        assert settlement.failure_reason is None
        assert (await finish(operation_store, running, settlement)).status == "completed"
        latest = await service.get_current_state(session_id)
        assert latest is not None
        recovered = state_from_record(latest)

        arguments = _node_args(recovered, view, snapshot, "assess", omit=True)
        arguments["options"]["prompt_template"] = "Reassess {{ row.ticket_id }}: {{ row.body }}."
        edited = _write("upsert_node", arguments, recovered, view, snapshot)
        assert edited.success, edited.to_dict()
        assert edited.updated_state.version == recovered.version + 1
        intermediate = await _finalize_completed_incremental_mutation(edited, recovered, plugin_snapshot=snapshot, policy_catalog=view)
        assert not intermediate.validation.is_valid
        assert all(node.id != node_id for node in intermediate.updated_state.nodes)
        validation = validate_pipeline_for_trained_operator(
            intermediate.updated_state,
            _validation_settings(str(tmp_path)),
            _FakeYamlGenerator(),
            plugin_snapshot=snapshot,
            profile_registry=view._profiles,
            catalog=view._full,
        )
        assert not validation.is_valid
        assert "required_control_coverage" in {error.error_code for error in validation.errors}

        arguments["on_success"] = "assessed"
        repaired = _write("upsert_node", arguments, intermediate.updated_state, view, snapshot)
        assert repaired.success, repaired.to_dict()
        finalized = await _finalize_completed_incremental_mutation(
            repaired, intermediate.updated_state, plugin_snapshot=snapshot, policy_catalog=view
        )
        assert _row(finalized.updated_state, node_id)["status"] == "pending"
        removal = _write("remove_node", {"id": node_id}, finalized.updated_state, view, snapshot)
        assert not removal.success
        assert CODE in {error.error_code for error in removal.validation.errors}
    finally:
        repository.release(running.session_operation_context)
