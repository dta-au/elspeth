"""Approved prompts remain editable through the ordinary option-patch tool."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompts import build_context_string, project_server_owned_option_metadata
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec
from elspeth.web.composer.tools import sessions as session_tools
from elspeth.web.composer.tools import transforms as transform_tools
from elspeth.web.composer.tools._common import _serialize_set_pipeline_arguments
from elspeth.web.interpretation_state import (
    InterpretationReviewPending,
    approved_prompt_artifact_hash_from_options,
    materialize_state_for_authoring,
    materialize_state_for_execution,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
    reconcile_authoritative_reviews,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_tools import _empty_state, _llm_options_with_forged_resolved_reviews, _mock_catalog, execute_tool


def _amended_approved_state(*, pending_audience: bool = False, literal_prefix: str = "") -> CompositionState:
    """Use owned render/hash producers for two amended interpretation references."""
    state = _approved_state(structured=True)
    options = deep_thaw(state.nodes[0].options)
    options["prompt_template_parts"][0]["text"] = literal_prefix + options["prompt_template_parts"][0]["text"]
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    category["draft"] = "old rejected broad categories"
    category["display_title"] = "Category definition"
    options["prompt_template_parts"].extend(
        [{"kind": "text", "text": " Audience: "}, {"kind": "interpretation_ref", "requirement_id": "audience"}]
    )
    options["interpretation_requirements"].append(
        {
            "id": "audience",
            "kind": "vague_term",
            "user_term": "audience",
            "draft": "everyone",
            "status": "pending" if pending_audience else "resolved",
            "event_id": None if pending_audience else "audience-event",
            "accepted_value": None if pending_audience else "new users",
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None if pending_audience else stable_hash("new users"),
        }
    )
    state = materialize_state_for_authoring(state.with_node(replace(state.nodes[0], options=options)))
    options = deep_thaw(state.nodes[0].options)
    prompt_review = next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")
    prompt_review["draft"] = prompt_review_draft_from_options(options)
    prompt_review["status"] = "pending" if pending_audience else "resolved"
    prompt_review["event_id"] = None if pending_audience else "prompt-event"
    prompt_review["accepted_value"] = None if pending_audience else prompt_review["draft"]
    prompt_review["resolved_prompt_template_hash"] = None if pending_audience else prompt_review_anchor_hash_from_options(options)
    if pending_audience:
        del options["approved_prompt_artifact_hash"]
    else:
        options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    state = state.with_node(replace(state.nodes[0], options=options))
    # The real reconciler derives the current artifact even when another
    # required review remains pending; that digest does not admit execution.
    return reconcile_authoritative_reviews(state, state)


@pytest.mark.parametrize("pending_audience", [False, True])
@pytest.mark.parametrize("writer", ["set_pipeline", "upsert_node", "patch_node_options"])
@pytest.mark.parametrize("echo", ["current", "full", "stale-resolved", "tampered-history"])
def test_current_projection_roundtrip_preserves_amendments_and_review_authority(writer: str, pending_audience: bool, echo: str) -> None:
    state = _amended_approved_state(pending_audience=pending_audience)
    before = state.to_dict()
    before_hash = composition_content_hash(state)
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    visible = json.loads(build_context_string(state, policy, plugin_snapshot=snapshot).split("\n", 1)[1])["current_state"]
    node = visible["nodes"][0]
    options = node["options"]
    original = state.nodes[0].options
    assert "prompt_template_parts" in options
    assert options["prompt_template_parts"] == deep_thaw(original["prompt_template_parts"])
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    assert category == {
        "id": "category",
        "kind": "vague_term",
        "user_term": "category",
        "historical_draft": "old rejected broad categories",
        "status": "resolved",
        "display_title": "Category definition",
    }
    assert "support categories" in options["prompt_template"]
    assert "old rejected broad categories" not in options["prompt_template"]
    if echo == "full":
        options["interpretation_requirements"] = deep_thaw(original["interpretation_requirements"])
    elif echo == "stale-resolved":
        category["draft"] = category.pop("historical_draft")
    elif echo == "tampered-history":
        category["historical_draft"] = "PRIVATE-HISTORY-TAMPER"
    if writer == "set_pipeline":
        # A substantive routing-only change, rather than an identical echo.
        node["on_error"] = "main"
        args, error = _serialize_set_pipeline_arguments(state)
        assert error is None and args is not None
        # The read view's output.name is a display field; the canonical
        # authoring exporter supplies sink_name while this node comes from
        # the actual current context, including its exact review projection.
        args["source"] = visible["sources"]["source"]
        args["nodes"] = visible["nodes"]
    elif writer == "upsert_node":
        args = {**node, "on_error": "main"}
    else:
        args = {"node_id": node["id"], "patch": options}
    result = execute_tool(writer, args, state, catalog)
    assert state.to_dict() == before and composition_content_hash(state) == before_hash
    if echo in ("stale-resolved", "tampered-history"):
        assert not result.success and result.updated_state is state
        assert result.validation.errors[0].error_code == "interpretation_requirements_invalid"
        assert "PRIVATE-HISTORY-TAMPER" not in repr(result.to_dict())
        return
    assert result.success, result.validation.errors
    updated = result.updated_state.nodes[0]
    assert updated.on_error == ("discard" if writer == "patch_node_options" else "main")
    assert updated.options == original
    assert state.to_dict() == before and composition_content_hash(state) == before_hash
    if pending_audience:
        assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)
    else:
        assert isinstance(materialize_state_for_execution(result.updated_state), CompositionState)
        assert updated.options["approved_prompt_artifact_hash"] == original["approved_prompt_artifact_hash"]


@pytest.mark.parametrize("damage", ["stale", "history", "accepted", "reference", "missing-parts", "stored-hash", "approval"])
def test_projected_review_corruption_refuses_atomically(damage: str) -> None:
    state = _amended_approved_state()
    if damage in ("stored-hash", "approval"):
        options = deep_thaw(state.nodes[0].options)
        if damage == "stored-hash":
            next(row for row in options["interpretation_requirements"] if row["id"] == "category")["resolved_prompt_template_hash"] = (
                stable_hash("different")
            )
        else:
            options["approved_prompt_artifact_hash"] = "a" * 64
        state = state.with_node(replace(state.nodes[0], options=options))
    before = state.to_dict()
    visible = project_server_owned_option_metadata(state.to_dict())
    node = visible["nodes"][0]
    options = node["options"]
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    if damage == "stale":
        category["draft"] = category.pop("historical_draft")
    elif damage == "history":
        category["historical_draft"] = "PRIVATE-HISTORY-TAMPER"
    elif damage == "accepted":
        category["accepted_value"] = "PRIVATE-FORGED-ACCEPTANCE"
    elif damage == "reference":
        options["prompt_template_parts"][1] = {"kind": "interpretation_ref", "requirement_id": "unknown"}
    elif damage == "missing-parts":
        del options["prompt_template_parts"]
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    args["source"] = visible["sources"]["source"]
    args["nodes"] = visible["nodes"]
    if damage == "approval":
        # The option-patch owner explicitly verifies the saved approval digest.
        result = execute_tool("patch_node_options", {"node_id": node["id"], "patch": options}, state, _mock_catalog())
    else:
        result = execute_tool("set_pipeline", args, state, _mock_catalog())
    assert not result.success
    assert result.updated_state is state
    assert state.to_dict() == before
    assert "PRIVATE-HISTORY-TAMPER" not in repr(result.to_dict())
    assert "PRIVATE-FORGED-ACCEPTANCE" not in repr(result.to_dict())


@pytest.mark.parametrize("field", ["prompt_template_parts", "system_prompt"])
def test_editing_current_amended_projection_reopens_only_applicable_reviews(field: str) -> None:
    state = _amended_approved_state()
    options = project_server_owned_option_metadata(state.to_dict())["nodes"][0]["options"]
    if field == "prompt_template_parts":
        value = [{"kind": "text", "text": "Assess "}, *options[field][1:]]
    else:
        value = "Explain the classification."
    before = state.to_dict()
    result = execute_tool("patch_node_options", {"node_id": "code_themes", "patch": {field: value}}, state, _mock_catalog())
    assert result.success, result.validation.errors
    reviews = result.updated_state.nodes[0].options["interpretation_requirements"]
    assert next(row for row in reviews if row["kind"] == "llm_prompt_template")["status"] == "pending"
    vague = [row for row in reviews if row["kind"] == "vague_term"]
    assert [row["status"] for row in vague] == (["pending", "pending"] if field == "prompt_template_parts" else ["resolved", "resolved"])
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)
    updated = result.updated_state.nodes[0].options
    if field == "prompt_template_parts":
        assert "approved_prompt_artifact_hash" not in updated
    else:
        # Retained vague-term evidence derives an artifact identity. The
        # pending prompt review above still blocks execution independently.
        assert updated["approved_prompt_artifact_hash"] == approved_prompt_artifact_hash_from_options(updated)
        assert updated["approved_prompt_artifact_hash"] != state.nodes[0].options["approved_prompt_artifact_hash"]
    assert state.to_dict() == before


def test_canonical_export_stays_distinct_from_current_amended_projection() -> None:
    state = _amended_approved_state()
    result = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _mock_catalog())
    assert result.success
    options = result.data["nodes"][0]["options"]
    assert "pending interpretation" in options["prompt_template"]
    assert "support categories" not in options["prompt_template"] and "new users" not in options["prompt_template"]
    assert options["prompt_template_parts"] == state.nodes[0].options["prompt_template_parts"]
    assert all(set(row) <= {"kind", "user_term", "draft", "display_title"} for row in options["interpretation_requirements"])
    reapplied = execute_tool("set_pipeline", deep_thaw(result.data), state, _mock_catalog())
    assert reapplied.success, reapplied.validation.errors
    assert reapplied.updated_state.nodes[0].options == state.nodes[0].options


def _approved_state(*, structured: bool = False) -> CompositionState:
    options = _llm_options_with_forged_resolved_reviews({"secret_ref": "OPENROUTER_API_KEY"})
    options["system_prompt"] = "Classify the row using the requested categories."
    options["required_input_fields"] = []
    if structured:
        options["prompt_template"] = "Classify support categories for {{ row.text }}."
        options["prompt_template_parts"] = [
            {"kind": "text", "text": "Classify "},
            {"kind": "interpretation_ref", "requirement_id": "category"},
            {"kind": "text", "text": " for {{ row.text }}."},
        ]
        options["interpretation_requirements"].append(
            {
                "id": "category",
                "kind": "vague_term",
                "user_term": "category",
                "status": "resolved",
                "draft": "support categories",
                "event_id": "category-event",
                "accepted_value": "support categories",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": stable_hash("support categories"),
            }
        )
    prompt_review = next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")
    prompt_review["draft"] = prompt_review_draft_from_options(options)
    prompt_review["accepted_value"] = prompt_review["draft"]
    prompt_review["resolved_prompt_template_hash"] = prompt_review_anchor_hash_from_options(options)
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return (
        _empty_state()
        .with_source(
            SourceSpec(
                plugin="csv",
                on_success="source_out",
                options={"path": "/data/input.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )
        .with_node(
            NodeSpec(
                id="code_themes",
                node_type="transform",
                plugin="llm",
                input="source_out",
                on_success="main",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        .with_output(
            OutputSpec(
                name="main", plugin="csv", options={"path": "/data/output.csv", "schema": {"mode": "observed"}}, on_write_failure="discard"
            )
        )
    )


@pytest.mark.parametrize("field", ["prompt_template", "system_prompt"])
def test_changed_approved_prompt_is_accepted_and_requires_fresh_review(field: str) -> None:
    state = _approved_state()
    original_options = state.nodes[0].options

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {field: "Summarize the row instead."}}, state, _mock_catalog()
    )

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options[field] == "Summarize the row instead."
    assert "approved_prompt_artifact_hash" not in options
    statuses = {row["kind"]: row["status"] for row in options["interpretation_requirements"]}
    assert statuses == {"llm_prompt_template": "pending", "llm_model_choice": "resolved"}
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)
    assert state.nodes[0].options == original_options


@pytest.mark.parametrize("noop", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_unrelated_or_identical_patch_retains_approval(noop: bool, structured: bool) -> None:
    state = _approved_state(structured=structured)
    patch = {"prompt_template": state.nodes[0].options["prompt_template"]} if noop else {"temperature": 0.1}

    result = execute_tool("patch_node_options", {"node_id": "code_themes", "patch": patch}, state, _mock_catalog())

    assert result.success, result.validation.errors
    assert result.updated_state.nodes[0].options["approved_prompt_artifact_hash"] == state.nodes[0].options["approved_prompt_artifact_hash"]
    assert result.updated_state.nodes[0].options["interpretation_requirements"] == state.nodes[0].options["interpretation_requirements"]


def test_parts_only_patch_validates_the_new_rendered_prompt() -> None:
    state = _approved_state(structured=True)
    parts = [{"kind": "text", "text": "Assess "}, *state.nodes[0].options["prompt_template_parts"][1:]]

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {"prompt_template_parts": parts}}, state, _mock_catalog()
    )

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options["prompt_template"].startswith("Assess ")
    assert options["prompt_template_parts"][1] == state.nodes[0].options["prompt_template_parts"][1]
    assert next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")["status"] == "pending"
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    assert category["status"] == "pending"
    assert category["draft"] == "support categories"
    assert category["accepted_value"] is None
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)


def test_system_prompt_edit_preserves_unchanged_structured_category_review() -> None:
    state = _approved_state(structured=True)

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {"system_prompt": "Explain the classification."}}, state, _mock_catalog()
    )

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options["system_prompt"] == "Explain the classification."
    reviews = {row["kind"]: row for row in options["interpretation_requirements"]}
    assert reviews["llm_prompt_template"]["status"] == "pending"
    assert reviews["vague_term"] == next(row for row in state.nodes[0].options["interpretation_requirements"] if row["id"] == "category")
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)


def test_invalid_option_with_prompt_edit_rejects_atomically() -> None:
    state = _approved_state()

    result = execute_tool(
        "patch_node_options",
        {"node_id": "code_themes", "patch": {"prompt_template": "New wording.", "temperature": "invalid"}},
        state,
        _mock_catalog(),
    )

    assert not result.success
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == "plugin_options_invalid"


def test_raw_prompt_edit_of_structured_node_explains_parts_authority() -> None:
    state = _approved_state(structured=True)

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {"prompt_template": "New wording."}}, state, _mock_catalog()
    )

    assert not result.success
    assert result.updated_state is state
    assert any(error.error_code == "prompt_template_parts_required" for error in result.validation.errors)
    assert "interpretation_ref" in result.validation.errors[0].message


@pytest.mark.parametrize("field", ["temperature", "prompt_template"])
def test_patch_does_not_heal_corrupt_stored_approval(field: str) -> None:
    state = _approved_state()
    state = state.with_node(replace(state.nodes[0], options={**state.nodes[0].options, "approved_prompt_artifact_hash": "a" * 64}))
    value = 0.1 if field == "temperature" else "New wording."

    result = execute_tool("patch_node_options", {"node_id": "code_themes", "patch": {field: value}}, state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state


def test_patch_cannot_supply_replacement_approval_digest() -> None:
    state = _approved_state()

    result = execute_tool(
        "patch_node_options",
        {"node_id": "code_themes", "patch": {"prompt_template": "New wording.", "approved_prompt_artifact_hash": "a" * 64}},
        state,
        _mock_catalog(),
    )

    assert not result.success
    assert result.updated_state is state
    assert "runtime-owned" in result.validation.errors[0].message


# ---------------------------------------------------------------------------
# Replacing a structured prompt with new text (maintainer ruling 2026-11-19:
# "if I am telling it to replace A with B, it doesn't matter what is already
# there"). The signal is the model's: prompt_template as text together with
# prompt_template_parts: null.
# ---------------------------------------------------------------------------

_WRITERS = ("patch_node_options", "upsert_node", "set_pipeline")
_REPLACEMENT_TEXT = "Summarize {{ row.text }} in one line."
_FILE_MARKER = {"blob_ref": "00000000-0000-4000-8000-000000000001", "mode": "inline_content", "sha256": "a" * 64}


def _forced_refusal(*_args: object, **_kwargs: object) -> str:
    return "forced refusal"


def _forced_coded_refusal(*_args: object, **_kwargs: object) -> tuple[str, str]:
    return ("forced refusal", "plugin_options_invalid")


def _forced_value_error(*_args: object, **_kwargs: object) -> None:
    raise ValueError("forced refusal")


def _node(state: CompositionState, node_id: str = "code_themes") -> NodeSpec:
    return next(node for node in state.nodes if node.id == node_id)


def _vague_ids(state: CompositionState, node_id: str = "code_themes") -> list[str]:
    return [row["id"] for row in _node(state, node_id).options["interpretation_requirements"] if row["kind"] == "vague_term"]


def _replace_call(
    writer: str,
    state: CompositionState,
    *,
    node_id: str = "code_themes",
    prompt: object = _REPLACEMENT_TEXT,
    parts_null: bool = True,
    requirements: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """One writer's whole-prompt replace call.

    ``patch_node_options`` sends only the keys it changes. The whole-node
    writers send the node from the canonical authoring export, with the old
    prompt's parts and rows left out unless ``requirements`` carries rows.
    """
    written: dict[str, object] = {"prompt_template": prompt}
    if parts_null:
        written["prompt_template_parts"] = None
    if requirements is not None:
        written["interpretation_requirements"] = requirements
    if writer == "patch_node_options":
        return {"node_id": node_id, "patch": written}
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    node = next(item for item in args["nodes"] if item["id"] == node_id)
    kept = {key: value for key, value in node["options"].items() if key not in ("prompt_template_parts", "interpretation_requirements")}
    node["options"] = {**kept, **written}
    return node if writer == "upsert_node" else args


def _echoed_rows(state: CompositionState, form: str) -> list[dict[str, object]]:
    """The node's requirement rows as a model that carries them forward would send them."""
    if form == "stored":
        return deep_thaw(state.nodes[0].options["interpretation_requirements"])
    if form == "export":
        args, error = _serialize_set_pipeline_arguments(state)
        assert error is None and args is not None
        return args["nodes"][0]["options"]["interpretation_requirements"]
    assert form == "context"
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    visible = json.loads(build_context_string(state, policy, plugin_snapshot=snapshot).split("\n", 1)[1])["current_state"]
    return visible["nodes"][0]["options"]["interpretation_requirements"]


def _assert_prompt_replaced(
    result: object,
    *,
    superseded: list[str],
    node_id: str = "code_themes",
    prompt: str = _REPLACEMENT_TEXT,
) -> None:
    assert result.success, result.validation.errors
    options = _node(result.updated_state, node_id).options
    assert options["prompt_template"] == prompt
    assert "prompt_template_parts" not in options
    assert "approved_prompt_artifact_hash" not in options
    reviews = {row["kind"]: row for row in options["interpretation_requirements"]}
    assert sorted(row["kind"] for row in options["interpretation_requirements"]) == ["llm_model_choice", "llm_prompt_template"]
    prompt_review = reviews["llm_prompt_template"]
    assert prompt_review["status"] == "pending"
    assert prompt_review["draft"] == prompt_review_draft_from_options(options)
    assert prompt in prompt_review["draft"]
    assert prompt_review["event_id"] is None and prompt_review["accepted_value"] is None
    assert prompt_review["resolved_prompt_template_hash"] is None
    assert reviews["llm_model_choice"]["status"] == "resolved"
    assert list(result.data["superseded_review_ids"]) == superseded
    # Nothing approved for the old prompt admits the new one to a run.
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)


def _slot_fixture(slots: str) -> CompositionState:
    if slots == "accepted":
        return _approved_state(structured=True)
    if slots == "two-accepted":
        return _amended_approved_state()
    assert slots == "accepted-and-pending"
    return _amended_approved_state(pending_audience=True)


@pytest.mark.parametrize("writer", _WRITERS)
@pytest.mark.parametrize("slots", ["accepted", "two-accepted", "accepted-and-pending"])
def test_text_with_null_parts_replaces_a_structured_prompt(writer: str, slots: str) -> None:
    state = _slot_fixture(slots)
    before = state.to_dict()
    before_hash = composition_content_hash(state)
    superseded = _vague_ids(state)
    assert superseded, "the fixture carries the slot reviews this test retires"

    result = execute_tool(writer, _replace_call(writer, state), state, _mock_catalog())

    _assert_prompt_replaced(result, superseded=superseded)
    assert state.to_dict() == before and composition_content_hash(state) == before_hash


def test_replacing_a_prompt_whose_only_slot_is_pending() -> None:
    state = _approved_state(structured=True)
    options = deep_thaw(state.nodes[0].options)
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    category.update(status="pending", event_id=None, accepted_value=None, resolved_prompt_template_hash=None)
    del options["approved_prompt_artifact_hash"]
    state = state.with_node(replace(state.nodes[0], options=options))
    state = reconcile_authoritative_reviews(state, state)
    assert next(row for row in state.nodes[0].options["interpretation_requirements"] if row["id"] == "category")["status"] == "pending"

    result = execute_tool("patch_node_options", _replace_call("patch_node_options", state), state, _mock_catalog())

    _assert_prompt_replaced(result, superseded=["category"])


@pytest.mark.parametrize("writer", _WRITERS)
@pytest.mark.parametrize("form", ["stored", "export", "context"])
@pytest.mark.parametrize("slots", ["two-accepted", "accepted-and-pending"])
def test_replace_call_that_carries_the_old_prompts_rows_still_replaces(writer: str, form: str, slots: str) -> None:
    """The skill tells the model to carry requirement rows forward; a replace call must not be refused for it."""
    state = _slot_fixture(slots)
    before = state.to_dict()
    echoed = _echoed_rows(state, form)
    assert sum(1 for row in echoed if row["kind"] == "vague_term") == 2

    result = execute_tool(writer, _replace_call(writer, state, requirements=echoed), state, _mock_catalog())

    _assert_prompt_replaced(result, superseded=["category", "audience"])
    assert state.to_dict() == before


@pytest.mark.parametrize("reuse_term", [False, True])
def test_structured_prompt_replaced_by_another_structured_prompt_in_one_call(reuse_term: bool) -> None:
    """Structured to structured is the ordinary path: new parts with the new prompt's full requirement list."""
    state = _approved_state(structured=True)
    term = "category" if reuse_term else "tone"
    patch = {
        "prompt_template": "Rate the formal register of {{ row.text }}.",
        "prompt_template_parts": [
            {"kind": "text", "text": "Rate the "},
            # A reused term keeps its stored id; a new one gets the server-projected "<term>:<node id>".
            {"kind": "interpretation_ref", "requirement_id": "category" if reuse_term else "tone:code_themes"},
            {"kind": "text", "text": " of {{ row.text }}."},
        ],
        "interpretation_requirements": [{"kind": "vague_term", "user_term": term, "draft": "formal register"}],
    }

    result = execute_tool("patch_node_options", {"node_id": "code_themes", "patch": patch}, state, _mock_catalog())

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    vague = [row for row in options["interpretation_requirements"] if row["kind"] == "vague_term"]
    assert [(row["user_term"], row["status"], row["draft"]) for row in vague] == [(term, "pending", "formal register")]
    assert vague[0]["accepted_value"] is None and vague[0]["event_id"] is None
    assert options["prompt_template_parts"][1]["requirement_id"] == vague[0]["id"]
    assert next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")["status"] == "pending"
    assert "approved_prompt_artifact_hash" not in options
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)


@pytest.mark.parametrize("writer", _WRITERS)
def test_changed_text_without_the_null_parts_signal_is_refused_and_names_the_replace_call(writer: str) -> None:
    state = _approved_state(structured=True)

    result = execute_tool(writer, _replace_call(writer, state, parts_null=False), state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state
    error = next(error for error in result.validation.errors if error.error_code == "prompt_template_parts_required")
    assert "prompt_template_parts: null" in error.message
    assert "interpretation_ref" in error.message


def _two_structured_nodes() -> CompositionState:
    """code_themes -> second, each an approved structured prompt with its own accepted slot."""
    state = _approved_state(structured=True)
    first = state.nodes[0]
    second_options = deep_thaw(first.options)
    for row in second_options["interpretation_requirements"]:
        row["id"] = row["id"].replace("code_themes", "second")
        row["user_term"] = row["user_term"].replace("code_themes", "second")
    second = replace(first, id="second", input="themes_out", on_success="main", options=second_options)
    state = state.with_node(replace(first, on_success="themes_out")).with_node(second)
    return reconcile_authoritative_reviews(state, state)


@pytest.mark.parametrize("writer", _WRITERS)
def test_replacing_one_nodes_prompt_leaves_another_structured_node_untouched(writer: str) -> None:
    state = _two_structured_nodes()
    untouched = _node(state, "second").options
    control = execute_tool("patch_node_options", {"node_id": "second", "patch": {"temperature": 0.1}}, state, _mock_catalog())
    assert control.success, control.validation.errors

    result = execute_tool(writer, _replace_call(writer, state), state, _mock_catalog())

    _assert_prompt_replaced(result, superseded=["category"])
    assert _node(result.updated_state, "second").options == untouched
    assert _vague_ids(result.updated_state, "second") == ["category"]
    assert "prompt_template_parts" in _node(result.updated_state, "second").options


def test_set_pipeline_replacing_two_prompts_names_every_superseded_review() -> None:
    state = _two_structured_nodes()
    args = _replace_call("set_pipeline", state)
    second = next(item for item in args["nodes"] if item["id"] == "second")
    second["options"] = {
        **{key: value for key, value in second["options"].items() if key != "interpretation_requirements"},
        "prompt_template": "Translate {{ row.text }}.",
        "prompt_template_parts": None,
    }

    result = execute_tool("set_pipeline", args, state, _mock_catalog())

    assert result.success, result.validation.errors
    assert list(result.data["superseded_review_ids"]) == ["category", "category"]
    for node_id, prompt in (("code_themes", _REPLACEMENT_TEXT), ("second", "Translate {{ row.text }}.")):
        options = _node(result.updated_state, node_id).options
        assert options["prompt_template"] == prompt
        assert "prompt_template_parts" not in options
        assert _vague_ids(result.updated_state, node_id) == []


_REFUSAL_SEAMS = [
    ("_structured_prompt_template_refusal", _forced_refusal, "prompt_template_parts_required"),
    ("_canonical_interpretation_requirement_error", _forced_refusal, "interpretation_requirements_invalid"),
    ("_prevalidate_transform_for_context", _forced_coded_refusal, "plugin_options_invalid"),
    ("_post_mutation_invariant_error", _forced_coded_refusal, "plugin_options_invalid"),
    ("reconcile_authoritative_reviews", _forced_value_error, "review_reconciliation_failed"),
    ("composition_review_contract_error", _forced_refusal, "interpretation_review_contract_unsatisfied"),
]


@pytest.mark.parametrize("writer", _WRITERS)
@pytest.mark.parametrize(("seam", "forced", "code"), _REFUSAL_SEAMS, ids=[seam for seam, _forced, _code in _REFUSAL_SEAMS])
def test_a_refusal_after_the_replacement_is_decided_returns_the_original_state(
    monkeypatch: pytest.MonkeyPatch, writer: str, seam: str, forced: object, code: str
) -> None:
    """Every refusal on the replacement path hands back the caller's own state object, parts and reviews intact."""
    state = _approved_state(structured=True)
    before = state.to_dict()
    args = _replace_call(writer, state)
    monkeypatch.setattr(session_tools if writer == "set_pipeline" else transform_tools, seam, forced)

    result = execute_tool(writer, args, state, _mock_catalog())

    assert not result.success
    assert {error.error_code for error in result.validation.errors} == {code}
    assert result.updated_state is state
    assert state.to_dict() == before
    assert "prompt_template_parts" in state.nodes[0].options and _vague_ids(state) == ["category"]


@pytest.mark.parametrize("writer", _WRITERS)
def test_byte_identical_replacement_text_still_reopens_the_prompt_review(writer: str) -> None:
    """Nothing approved for A approves B, even when B renders to the same bytes."""
    state = _approved_state(structured=True)
    same_text = state.nodes[0].options["prompt_template"]
    assert next(row for row in state.nodes[0].options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")["status"] == (
        "resolved"
    )

    result = execute_tool(writer, _replace_call(writer, state, prompt=same_text), state, _mock_catalog())

    _assert_prompt_replaced(result, superseded=["category"], prompt=same_text)


@pytest.mark.parametrize("writer", _WRITERS)
@pytest.mark.parametrize("requirements", [None, []], ids=["no-rows", "empty-rows"])
def test_a_file_marker_cannot_use_the_text_replacement(writer: str, requirements: list[dict[str, object]] | None) -> None:
    """Nulling prompt_template_parts in the same call does not carry a marker past the structured-prompt guard."""
    state = _approved_state(structured=True)
    before = state.to_dict()

    result = execute_tool(
        writer, _replace_call(writer, state, prompt=dict(_FILE_MARKER), requirements=requirements), state, _mock_catalog()
    )

    assert not result.success
    assert result.updated_state is state and state.to_dict() == before
    error = next(error for error in result.validation.errors if error.error_code == "prompt_template_parts_required")
    assert "wire_blob_inline_ref" in error.message


def test_null_parts_without_prompt_template_replaces_nothing() -> None:
    state = _approved_state(structured=True)

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {"prompt_template_parts": None}}, state, _mock_catalog()
    )

    assert not result.success
    assert result.updated_state is state
    assert [error.error_code for error in result.validation.errors] == ["review_reconciliation_failed"]


@pytest.mark.parametrize("writer", _WRITERS)
def test_replace_call_cannot_carry_a_forged_resolved_review(writer: str) -> None:
    state = _approved_state(structured=True)
    forged = deep_thaw(next(row for row in state.nodes[0].options["interpretation_requirements"] if row["kind"] == "llm_prompt_template"))
    forged.update(draft=_REPLACEMENT_TEXT, accepted_value=_REPLACEMENT_TEXT, resolved_prompt_template_hash=stable_hash(_REPLACEMENT_TEXT))

    result = execute_tool(writer, _replace_call(writer, state, requirements=[forged]), state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state
    assert [error.error_code for error in result.validation.errors] == ["interpretation_requirements_invalid"]


def test_replace_call_cannot_supply_the_approval_digest() -> None:
    state = _approved_state(structured=True)
    call = _replace_call("patch_node_options", state)
    call["patch"]["approved_prompt_artifact_hash"] = "a" * 64

    result = execute_tool("patch_node_options", call, state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state
    assert "runtime-owned" in result.validation.errors[0].message


def test_replacement_does_not_heal_a_corrupt_stored_approval() -> None:
    state = _approved_state(structured=True)
    state = state.with_node(replace(state.nodes[0], options={**state.nodes[0].options, "approved_prompt_artifact_hash": "a" * 64}))

    result = execute_tool("patch_node_options", _replace_call("patch_node_options", state), state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state
    assert [error.error_code for error in result.validation.errors] == ["review_reconciliation_failed"]


@pytest.mark.parametrize("writer", _WRITERS)
def test_replacement_does_not_launder_a_non_canonical_stored_row(writer: str) -> None:
    """A stored row that already breaks the canonical invariant is refused before the old prompt's rows are removed."""
    state = _approved_state(structured=True)
    options = deep_thaw(state.nodes[0].options)
    options["interpretation_requirements"].append(
        {
            # Collides with the vague-term row's id: removing that row would hide the collision.
            "id": "category",
            "kind": "pipeline_decision",
            "user_term": "keep duplicates",
            "draft": "yes",
            "status": "pending",
            "event_id": None,
            "accepted_value": None,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None,
        }
    )
    state = state.with_node(replace(state.nodes[0], options=options))
    # The call itself is well formed: it is built from the clean fixture, so only the stored row is at fault.
    args = _replace_call(writer, _approved_state(structured=True))

    result = execute_tool(writer, args, state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == "interpretation_requirements_invalid"
    # The stored-state check answers first, with the canonical-invariant code;
    # reconciliation against the untouched state would refuse it second.
    assert "canonical interpretation requirements failed" in result.validation.errors[0].message


def test_the_replace_signal_on_a_non_llm_node_strips_nothing() -> None:
    state = _approved_state(structured=True)
    state = state.with_node(replace(state.nodes[0], plugin="passthrough"))
    before = state.to_dict()

    result = execute_tool("patch_node_options", _replace_call("patch_node_options", state), state, _mock_catalog())

    if result.success:
        options = result.updated_state.nodes[0].options
        assert result.data is None or "superseded_review_ids" not in result.data
        assert [row["id"] for row in options["interpretation_requirements"] if row["kind"] == "vague_term"] == ["category"]
    else:
        assert result.updated_state is state
    assert state.to_dict() == before


@pytest.mark.parametrize("writer", _WRITERS)
def test_the_replace_signal_on_an_unstructured_prompt_is_an_ordinary_edit(writer: str) -> None:
    state = _approved_state()

    result = execute_tool(writer, _replace_call(writer, state), state, _mock_catalog())

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options["prompt_template"] == _REPLACEMENT_TEXT
    assert result.data is None or "superseded_review_ids" not in result.data
    statuses = {row["kind"]: row["status"] for row in options["interpretation_requirements"]}
    assert statuses == {"llm_prompt_template": "pending", "llm_model_choice": "resolved"}


def test_accepting_a_proposed_replace_call_replays_it() -> None:
    """Proposal acceptance replays the stored tool call through the same dispatcher."""
    state = _approved_state(structured=True)
    args = _replace_call("patch_node_options", state)

    direct = execute_tool("patch_node_options", args, state, _mock_catalog())
    replayed = execute_tool("patch_node_options", args, state, _mock_catalog(), executing_proposal_id="proposal-1")

    _assert_prompt_replaced(replayed, superseded=["category"])
    assert replayed.updated_state.nodes[0].options == direct.updated_state.nodes[0].options
