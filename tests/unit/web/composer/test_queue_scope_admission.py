"""Queue scope values reach the intrinsic guard without changing saved graphs."""

from __future__ import annotations

from copy import deepcopy
from itertools import combinations
from pathlib import Path
from typing import Any, Literal

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import execute_tool, transforms
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from tests.unit.web.composer.test_set_pipeline_candidate import _empty_state, _named_multi_source_queue_args, _trained_context
from tests.unit.web.composer.test_transform_rejection_contracts import _assert_refusal, _saved_state

QUEUE = {"id": "inbound", "node_type": "queue", "input": "inbound"}
SCOPE = {"scope_name": "document_pages", "scope_opener": "expand_rows", "scope_policy": "require_all"}
SCOPE_CASES = (
    [
        {key: (policy if key == "scope_policy" else SCOPE[key]) for key in keys}
        for size in range(1, 4)
        for keys in combinations(SCOPE, size)
        for policy in (["require_all", "best_effort"] if "scope_policy" in keys else ["require_all"])
    ]
    + [{**dict.fromkeys(SCOPE), key: value} for key, value in SCOPE.items()]
    + [{"scope_name": ""}, {"scope_opener": ""}]
)
MALFORMED_SCOPE_CASES = [{key: value} for key in SCOPE for value in (42, [], {})] + [
    {"scope_policy": "all"},
    {"unknown_scope": "document_pages"},
]


def scope_message(override: dict[str, Any]) -> str:
    present = sorted(key for key, value in override.items() if value is not None)
    return f"Queue 'inbound' does not accept field(s): {present}."


def private_snapshot(state: CompositionState) -> tuple[dict[str, Any], str, str, int]:
    content_hash = composition_content_hash(state)
    data = state.to_dict()
    return deepcopy(data), stable_hash(data), content_hash, state.version


def dispatch(tool: str, args: dict[str, Any], state: CompositionState, context: ToolContext, **kwargs: Any) -> ToolResult:
    return execute_tool(
        tool,
        args,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=context.data_dir,
        session_id=context.session_id,
        validate_arguments=True,
        **kwargs,
    )


def complete_queue(tmp_path: Path, *, session_id: str = "test-session") -> tuple[dict[str, Any], CompositionState, ToolContext]:
    """Real catalog and complete graph with source paths in the current session."""
    args = _named_multi_source_queue_args(tmp_path)
    for name, source in args["sources"].items():
        path = tmp_path / "blobs" / session_id / f"{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value\n1\n", encoding="utf-8")
        source["options"]["path"] = str(path)
    context = _trained_context(data_dir=tmp_path, session_id=session_id)
    built = dispatch("set_pipeline", args, _empty_state(), context)
    assert built.success and built.validation.is_valid, built.to_dict()
    canonical = dispatch("upsert_node", args["nodes"][0], built.updated_state, context)
    assert canonical.success and canonical.validation.is_valid, canonical.to_dict()
    return args, canonical.updated_state, context


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("existing_queue", [False, True])
@pytest.mark.parametrize("override", SCOPE_CASES)
def test_scope_refusal_preserves_full_private_saved_state(
    surface: Literal["handler", "dispatch"],
    existing_queue: bool,
    override: dict[str, Any],
) -> None:
    state = _saved_state()
    context = _trained_context()
    if existing_queue:
        canonical = dispatch("upsert_node", {**QUEUE, "options": {"description": "Keep this queue"}}, state, context)
        assert canonical.success
        state = canonical.updated_state
    before = private_snapshot(state)
    standing = state.validate()
    args = {**QUEUE, "description": "Must not replace", **override}
    result = transforms._handle_upsert_node(args, state, context) if surface == "handler" else dispatch("upsert_node", args, state, context)
    _assert_refusal(result, state, "queue_config_invalid", "node:inbound", standing)
    assert result.validation.errors[0].message == scope_message(override)
    assert result.applied_component is None
    assert private_snapshot(state) == before
    assert private_snapshot(result.updated_state) == before


@pytest.mark.parametrize("override", SCOPE_CASES)
def test_complete_real_routes_agree_on_intrinsic_refusal(tmp_path: Path, override: dict[str, Any]) -> None:
    args, state, context = complete_queue(tmp_path)
    before = private_snapshot(state)
    args["nodes"][0].update(override)
    incremental = dispatch("upsert_node", args["nodes"][0], state, context)
    bulk = dispatch("set_pipeline", args, state, context)
    for result in (incremental, bulk):
        assert not result.success and result.updated_state is state
        assert result.affected_nodes == () and result.applied_component is None
        entry = result.validation.errors[0]
        assert entry.error_code == "queue_config_invalid" and entry.rejected_component == "node:inbound"
        assert entry.message.endswith(scope_message(override))
        assert private_snapshot(result.updated_state) == before
    assert len(bulk.validation.errors) == 1


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("override", [{}, dict.fromkeys(SCOPE), {"options": {"description": "interleave"}}, {"description": "Queue notes"}])
def test_canonical_orphan_and_changed_description_remain_admitted(surface: str, override: dict[str, Any]) -> None:
    context = _trained_context()
    state = _empty_state()
    args = {**QUEUE, **override}
    invoke = (
        (lambda payload, prior: transforms._handle_upsert_node(payload, prior, context))
        if surface == "handler"
        else (lambda payload, prior: dispatch("upsert_node", payload, prior, context))
    )
    result = invoke(args, state)
    assert result.success and not result.validation.is_valid
    assert result.updated_state.version == state.version + 1 and "inbound" in result.affected_nodes
    (queue,) = result.updated_state.nodes
    assert queue.id == queue.input == "inbound"
    assert queue.plugin is queue.on_success is queue.on_error is None
    assert queue.condition is queue.routes is queue.fork_to is queue.branches is None
    assert queue.policy is queue.merge is queue.trigger is queue.output_mode is None
    assert queue.expected_output_count is queue.timeout_seconds is None
    assert queue.scope_name is queue.scope_opener is queue.scope_policy is None
    assert dict(queue.options) == override.get("options", {})
    assert queue.description == override.get("description")
    changed = invoke({**args, "description": "Changed description"}, result.updated_state)
    assert changed.success and changed.updated_state.version == result.updated_state.version + 1
    assert changed.updated_state.nodes[0].description == "Changed description"


@pytest.mark.parametrize("override", MALFORMED_SCOPE_CASES)
def test_malformed_scope_retains_existing_argument_channels(override: dict[str, Any]) -> None:
    state, context = _saved_state(), _trained_context()
    before = private_snapshot(state)
    args = {**QUEUE, **override}
    with pytest.raises(ToolArgumentError) as caught:
        transforms._handle_upsert_node(args, state, context)
    assert caught.value.category == ToolArgumentErrorCategory.MODEL_VALIDATION
    with pytest.raises(ToolArgumentError) as caught:
        execute_tool("upsert_node", args, state, context.catalog, plugin_snapshot=context.plugin_snapshot)
    assert caught.value.category == ToolArgumentErrorCategory.MODEL_VALIDATION
    ordinary = dispatch("upsert_node", args, state, context)
    assert not ordinary.success and ordinary.updated_state is state
    with pytest.raises(ToolArgumentError) as caught:
        dispatch("upsert_node", args, state, context, raise_schema_argument_errors=True)
    assert caught.value.category == ToolArgumentErrorCategory.SCHEMA_SHAPE
    assert private_snapshot(state) == before


def test_bulk_arbitrary_policy_still_reaches_intrinsic_guard(tmp_path: Path) -> None:
    args, state, context = complete_queue(tmp_path)
    args["nodes"][0]["scope_policy"] = "all"
    result = dispatch("set_pipeline", args, state, context)
    assert not result.success and result.updated_state is state
    assert result.validation.errors[0].error_code == "queue_config_invalid"
    assert result.validation.errors[0].message.endswith(scope_message({"scope_policy": "all"}))
