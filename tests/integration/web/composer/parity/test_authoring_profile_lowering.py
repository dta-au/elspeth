"""Companion to the readiness-preflight probe: the incremental transform
authoring tools must not false-positive on an operator-profiled multi-query LLM
node either.

``set_pipeline`` already guards the sequential-multi-query retry-budget /
managed-identity policy behind ``if "profile" not in node_options`` — an operator
profile injects the web-safe retry budget only at LOWERING, and the profile-
lowered executable is validated by ``_prevalidate_transform_for_context`` (which
lowers first). The incremental tools ``upsert_node`` / ``patch_node_options`` /
``splice_transform`` ran the raw policy UNCONDITIONALLY, so a profiled multi-query
node was uncommittable through those surfaces — the same false-positive as the
execution readiness preflight, at the authoring surface. These regressions drive
the real profile-aware policy stack (as ``app.py`` wires it) and assert the
profiled node clears the retry-budget gate.

The NON-profiled rejection (the safety gate that must survive the guard) is
covered by ``test_tools.TestTransformLlmRetryBudgetPolicy`` — the parity web
policy requires the profile authoring form, so a direct provider/model ``llm``
node is rejected earlier as ``profile_unavailable`` and cannot reach the
retry-budget gate on this surface.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.auth.models import UserIdentity
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.composer.tools._common import ToolContext, _prevalidate_transform_for_context
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _profiled_multi_query_options() -> dict[str, Any]:
    return {
        "profile": "task-role",
        "required_input_fields": ["color_name"],
        "prompt_template": "Assess {{ row.color_name }}.",
        "queries": {
            "blue": {
                "input_fields": {"color_name": "color_name"},
                "response_format": "structured",
                "output_fields": [{"suffix": "amount", "type": "integer"}],
            }
        },
        "schema": {"mode": "observed"},
    }


_RETRY_BUDGET_MARKER = "sequential multi-query llm"


def _profiled_node(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "assess_blue",
        "node_type": "transform",
        "plugin": "llm",
        "input": "rows",
        "on_success": "assessed",
        "on_error": "discard",
        "options": options,
    }


def _tool_kwargs(parity_env: Any) -> dict[str, Any]:
    state = parity_env.app.state
    snapshot = state.plugin_snapshot_factory(UserIdentity(user_id="alice", username="alice"))
    policy = PolicyCatalogView(state.catalog_service, snapshot, state.operator_profile_registry)
    return {
        "policy": policy,
        "plugin_snapshot": snapshot,
        "data_dir": str(parity_env.data_dir),
        "session_engine": state.session_engine,
        "secret_service": None,
        "user_id": "alice",
    }


def _run(parity_env: Any, tool: str, args: dict[str, Any], state: CompositionState) -> Any:
    kwargs = _tool_kwargs(parity_env)
    return execute_tool(tool, args, state, kwargs.pop("policy"), **kwargs)


def test_upsert_node_admits_profiled_multi_query_llm(parity_env: Any) -> None:
    result = _run(parity_env, "upsert_node", _profiled_node(_profiled_multi_query_options()), _empty_state())
    assert result.success is True, result.data
    node = next(n for n in result.updated_state.nodes if n.id == "assess_blue")
    assert node.options.get("profile") == "task-role"
    assert "max_capacity_retry_seconds" not in node.options


def test_patch_node_options_rejects_authored_profile_temperature(parity_env: Any) -> None:
    created = _run(parity_env, "upsert_node", _profiled_node(_profiled_multi_query_options()), _empty_state())
    assert created.success is True, created.data

    patched = _run(
        parity_env,
        "patch_node_options",
        {"node_id": "assess_blue", "patch": {"temperature": 0.2}},
        created.updated_state,
    )
    assert patched.success is False
    node = next(n for n in created.updated_state.nodes if n.id == "assess_blue")
    assert "temperature" not in node.options
    assert node.options.get("profile") == "task-role"


def _splice_base_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "rows.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="before",
                node_type="transform",
                plugin="passthrough",
                input="rows",
                on_success="middle",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="after",
                node_type="transform",
                plugin="passthrough",
                input="middle",
                on_success="result",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(
            EdgeSpec(id="source-before", from_node="source", to_node="before", edge_type="on_success", label=None),
            EdgeSpec(id="before-after", from_node="before", to_node="after", edge_type="on_success", label=None),
            EdgeSpec(id="after-output", from_node="after", to_node="result", edge_type="on_success", label=None),
        ),
        outputs=(
            OutputSpec(
                name="result",
                plugin="json",
                options={"path": "out.jsonl", "schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=4,
    )


def test_splice_transform_clears_retry_budget_gate_for_profiled_multi_query(parity_env: Any) -> None:
    # The splice retry-budget gate (transforms.py:_execute_splice_transform) runs
    # BEFORE the whole-pipeline splice validation, so before the fix a profiled
    # multi-query node was rejected here with the retry-budget error. After the
    # fix the gate is cleared; the node reaches the orthogonal context-aware
    # splice validation (which may accept or reject the spliced *graph* for
    # unrelated reasons). Assert only that the retry-budget gate no longer fires.
    args = {
        "predecessor_id": "before",
        "successor_id": "after",
        "node": {
            "id": "assess_blue",
            "plugin": "llm",
            "options": _profiled_multi_query_options(),
            "on_error": "discard",
        },
    }
    result = _run(parity_env, "splice_transform", args, _splice_base_state())
    error = "" if result.success else str(result.validation.errors[0].message).lower()
    assert _RETRY_BUDGET_MARKER not in error, result.data


@pytest.mark.parametrize("tool", ["upsert_node", "patch_node_options", "splice_transform", "set_pipeline"])
@pytest.mark.parametrize("binding_unavailable", [False, True], ids=["wrong-alias", "binding-unavailable"])
def test_transform_profile_rejections_keep_their_distinct_codes(
    parity_env: Any, tool: str, binding_unavailable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = {**_profiled_multi_query_options(), "profile": "task-role" if binding_unavailable else "missing-role"}
    state = _empty_state()
    if tool == "patch_node_options":
        created = _run(parity_env, "upsert_node", _profiled_node(_profiled_multi_query_options()), state)
        assert created.success is True, created.validation
        state = created.updated_state
        args = {"node_id": "assess_blue", "patch": {"profile": options["profile"], "prompt_template": "Review {{ row.color_name }}."}}
    elif tool == "splice_transform":
        state = _splice_base_state()
        args = {
            "predecessor_id": "before",
            "successor_id": "after",
            "node": {"id": "assess_blue", "plugin": "llm", "options": options, "on_error": "discard"},
        }
    elif tool == "set_pipeline":
        args = {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "on_validation_failure": "discard",
                "options": {"path": str(parity_env.data_dir / "blobs" / "rows.csv"), "schema": {"mode": "observed"}},
            },
            "nodes": [_profiled_node(options)],
            "edges": [],
            "outputs": [],
        }
    else:
        args = _profiled_node(options)
    if binding_unavailable:
        # The frozen snapshot still grants the alias, but its resolver is now
        # unavailable. This must remain an operator-repairable policy failure,
        # unlike the author-correctable wrong-alias control.
        from elspeth.web.plugin_policy import validation

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise ValueError("profile_unavailable")

        monkeypatch.setattr(validation, "_lower_profile_options", refuse)
    if tool == "set_pipeline":
        kwargs = _tool_kwargs(parity_env)
        result = execute_tool(tool, args, state, kwargs["policy"], plugin_snapshot=kwargs["plugin_snapshot"])
    else:
        result = _run(parity_env, tool, args, state)
    assert result.success is False
    assert result.updated_state is state
    assert result.affected_nodes == ()
    assert result.validation.errors[0].error_code == ("profile_unavailable" if binding_unavailable else "plugin_options_invalid")


def test_transform_prevalidation_does_not_require_unrelated_source_or_sink(parity_env: Any) -> None:
    kwargs = _tool_kwargs(parity_env)
    original = kwargs["plugin_snapshot"]
    transform_id = PluginId("transform", "llm")
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=original.policy_hash,
        principal_scope=original.principal_scope,
        available=frozenset({transform_id}),
        unavailable=(),
        selected=(),
        usable_profile_aliases=((transform_id, ("task-role",)),),
        selected_profile_aliases=((transform_id, "task-role"),),
        binding_generation_fingerprint="transform-only-prevalidation",
    )
    policy = PolicyCatalogView(parity_env.app.state.catalog_service, snapshot, parity_env.app.state.operator_profile_registry)
    context = ToolContext(catalog=policy, plugin_snapshot=snapshot)
    assert _prevalidate_transform_for_context(context, "llm", _profiled_multi_query_options()) is None
    assert _prevalidate_transform_for_context(context, "llm", {**_profiled_multi_query_options(), "profile": "missing-role"}) is not None


# --------------------------------------------------------------------------- #
# Negative: credential-egress fields cannot be smuggled onto a profiled node   #
# --------------------------------------------------------------------------- #
#
# The profiled-node guard at each incremental authoring seam (upsert_node /
# patch_node_options / splice_transform) SKIPS
# ``_validate_transform_provider_config_policy`` when ``"profile"`` is present in
# the options (``transforms.py``: ``if "profile" not in options``) — the raw
# provider-config policy would false-positive on the absent private retry budget
# (the positive admits-tests above lock that skip). On a profiled ``llm`` node
# the provider binding — and therefore the destination the server-resolved
# api_key is sent to — comes from the operator profile at LOWERING; the public
# profile schema does not expose ``base_url`` / ``provider`` / ``endpoint``. With
# the provider-config-policy gate skipped, the ONLY thing rejecting a profiled
# node that ALSO carries one of those credential-egress / SSRF fields is
# ``_prevalidate_transform_for_context`` — it lowers the profile first, and the
# public profile schema rejects the smuggled key (surfaced as
# ``plugin_options_invalid`` for these malformed authored options).
#
# These negatives regression-lock that rejection at each of the three seams. The
# load-bearing invariant is ``success is False``: paired with the positive
# admits-tests (same base profiled options → ADMITTED), the smuggled field is
# provably what flips admit → reject, so a future edit that widened the public
# profile schema to expose one of these fields, or that reordered/skipped the
# guard so a profiled node bypasses prevalidation, is caught here.

_SMUGGLED_EGRESS_FIELDS = [
    ("base_url", "http://169.254.169.254/latest/meta-data/"),
    ("provider", "openrouter"),
    ("endpoint", "http://internal.attacker.example/v1"),
]


def _profiled_options_with(field: str, value: str) -> dict[str, Any]:
    options = _profiled_multi_query_options()
    options[field] = value
    return options


def _egress_rejection_error(result: Any) -> str:
    """Assert a rejected mutation and return its lowercased error message.

    Mirrors the profile-lowering rejection the incremental seams produce for a
    profiled node carrying a credential-egress field. ``success is False`` is the
    security invariant; the returned message lets the caller pin the current
    ``plugin_options_invalid`` mechanism.
    """
    assert result.success is False, result.data
    return str(result.validation.errors[0].message).lower()


@pytest.mark.parametrize("field,value", _SMUGGLED_EGRESS_FIELDS)
def test_upsert_node_rejects_smuggled_egress_on_profiled_llm(parity_env: Any, field: str, value: str) -> None:
    result = _run(parity_env, "upsert_node", _profiled_node(_profiled_options_with(field, value)), _empty_state())
    assert field in _egress_rejection_error(result), result.data
    assert result.validation.errors[0].error_code == "plugin_options_invalid"
    # Rejection is atomic: the egress-carrying node is never committed.
    assert all(n.id != "assess_blue" for n in result.updated_state.nodes), result.updated_state


@pytest.mark.parametrize("field,value", _SMUGGLED_EGRESS_FIELDS)
def test_patch_node_options_rejects_smuggled_egress_onto_profiled_llm(parity_env: Any, field: str, value: str) -> None:
    created = _run(parity_env, "upsert_node", _profiled_node(_profiled_multi_query_options()), _empty_state())
    assert created.success is True, created.data

    patched = _run(
        parity_env,
        "patch_node_options",
        {"node_id": "assess_blue", "patch": {field: value}},
        created.updated_state,
    )
    assert field in _egress_rejection_error(patched), patched.data
    assert patched.validation.errors[0].error_code == "plugin_options_invalid"
    # Rejection is atomic: the smuggled field is never merged into the node.
    node = next(n for n in patched.updated_state.nodes if n.id == "assess_blue")
    assert field not in node.options, node.options


@pytest.mark.parametrize("field,value", _SMUGGLED_EGRESS_FIELDS)
def test_splice_transform_rejects_smuggled_egress_on_profiled_llm(parity_env: Any, field: str, value: str) -> None:
    # The splice candidate prevalidation (``_prepare_transform_candidate``) lowers
    # the profile before the whole-pipeline splice validation, so the smuggled
    # egress field is rejected here as ``plugin_options_invalid`` rather than reaching
    # the orthogonal graph validation.
    args = {
        "predecessor_id": "before",
        "successor_id": "after",
        "node": {
            "id": "assess_blue",
            "plugin": "llm",
            "options": _profiled_options_with(field, value),
            "on_error": "discard",
        },
    }
    result = _run(parity_env, "splice_transform", args, _splice_base_state())
    assert field in _egress_rejection_error(result), result.data
    assert result.validation.errors[0].error_code == "plugin_options_invalid"
    # Rejection is atomic: the egress-carrying node is never spliced in.
    assert all(n.id != "assess_blue" for n in result.updated_state.nodes), result.updated_state
