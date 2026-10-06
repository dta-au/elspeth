"""Model discovery preserves scope and completeness through the actual planner."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.tools import generation
from elspeth.web.composer.tools._common import ToolContext
from tests.unit.web.composer.test_pipeline_planner import _pipeline, _plan, _response, _ScriptedCompletion


@pytest.fixture
def deferred_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    # Scope/completeness is the subject here. Each real worker/provider timer
    # remains bounded; dedicated planner tests exercise budget progression.
    monkeypatch.setattr(pipeline_planner, "_planner_deadline_time", lambda: 100.0)
    original = pipeline_planner.build_planner_authoring_aids

    def without_model_catalog(view):
        aids = original(view)
        aids.pop("model_catalog", None)
        return aids

    monkeypatch.setattr(pipeline_planner, "build_planner_authoring_aids", without_model_catalog)
    monkeypatch.setattr(
        generation,
        "read_litellm_model_list",
        lambda: ["anthropic/model-a", "anthropic/model-b", "openai/model-c", "prefixless", "openrouter/anthropic/bundled"],
    )
    monkeypatch.setattr(generation, "get_catalog_values", lambda _catalog: frozenset({"anthropic/live"}))


@pytest.mark.asyncio
@pytest.mark.usefixtures("deferred_model_catalog")
@pytest.mark.parametrize(
    "initial, followup, no_gain, expected_count",
    [
        pytest.param({"provider": "anthropic", "limit": 1}, {"provider": "anthropic", "limit": 2}, False, 2, id="truncated-expansion"),
        pytest.param({}, {"provider": "anthropic", "limit": 2}, False, 2, id="summary-to-identifiers"),
        pytest.param({"provider": "anthropic", "limit": 2}, {"provider": "openai", "limit": 1}, False, 1, id="other-provider"),
        pytest.param(
            {"provider": "anthropic", "limit": 2}, {"provider": "anthropic/model-a", "limit": 1}, True, None, id="complete-subset"
        ),
        pytest.param({"provider": "anthropic", "limit": 2}, {"provider": "anthropic", "limit": 10}, True, None, id="complete-expansion"),
        pytest.param({"provider": "anthropic", "limit": 1}, {"provider": "anthropic", "limit": 1}, True, None, id="exact-repeat"),
        pytest.param({}, {"limit": 10}, True, None, id="summary-repeat-ignores-limit"),
        pytest.param({"provider": "openrouter", "limit": 10}, {"provider": "openrouter/", "limit": 10}, True, None, id="live-alias"),
        pytest.param(
            {"provider": "openrouter", "limit": 10},
            {"provider": "openrouter/anthropic", "limit": 10},
            False,
            1,
            id="live-versus-bundled-prefix",
        ),
        pytest.param({"provider": "open", "limit": 10}, {"provider": "openrouter", "limit": 10}, False, 1, id="bundled-versus-live"),
        pytest.param({"provider": "", "limit": 10}, {"provider": "anthropic", "limit": 10}, False, 2, id="prefixless-is-subset"),
    ],
)
async def test_model_discovery_surface_preserves_gain_by_scope_and_completion(
    tmp_path: Path,
    tool_context: ToolContext,
    initial: dict,
    followup: dict,
    no_gain: bool,
    expected_count: int | None,
) -> None:
    completion = _ScriptedCompletion(
        _response(("list_models", initial)),
        _response(("list_models", followup)),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )

    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, information_aware=True)

    names = {tool["function"]["name"] for tool in completion.requests[1]["tools"]}
    assert "list_models" in names
    final_tool = [json.loads(message["content"]) for message in completion.requests[2]["messages"] if message["role"] == "tool"][-1]
    if no_gain:
        assert final_tool["error_code"] == "DISCOVERY_NO_GAIN"
    else:
        assert final_tool["success"] is True
        assert final_tool["data"]["count"] == expected_count
        assert len(final_tool["data"]["models"]) == expected_count
        assert final_tool["data"]["truncated"] is False


def test_model_discovery_request_predicates_keep_partial_scope_unresolved() -> None:
    policy = pipeline_planner.PlannerDiscoveryPolicy.initial()
    arguments = {"provider": "anthropic", "limit": 1}
    keys = pipeline_planner._tool_information_keys("list_models", arguments)
    manifest = policy.manifest.with_result(keys, available=True)

    assert all(manifest.covers(key) for key in keys)
    assert not any(
        manifest.covers(key) for key in pipeline_planner._tool_information_keys("list_models", {"provider": "anthropic", "limit": 2})
    )
    assert not any(
        manifest.covers(key) for key in pipeline_planner._tool_information_keys("list_models", {"provider": "openai", "limit": 1})
    )
    assert not any(manifest.covers(key) for key in pipeline_planner._tool_information_keys("list_models", {}))
    assert "list_models" in policy.with_manifest(manifest).discovery_tool_names


def test_full_authoring_catalog_covers_listing_scopes() -> None:
    policy = pipeline_planner.PlannerDiscoveryPolicy.initial(aid_supplied_information=frozenset({"model.catalog"}))
    for arguments in ({}, {"provider": "anthropic", "limit": 100}, {"provider": "", "limit": 1}):
        keys = pipeline_planner._tool_information_keys("list_models", arguments)
        assert all(policy.manifest.covers(key) for key in keys)
    assert "list_models" in policy.with_manifest(policy.manifest).discovery_tool_names


@pytest.mark.asyncio
@pytest.mark.usefixtures("deferred_model_catalog")
@pytest.mark.parametrize("admitted_complete", [False, True])
async def test_model_discovery_completion_uses_selected_response_instead_of_original_root(
    tmp_path: Path, tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch, admitted_complete: bool
) -> None:
    original = pipeline_planner.admit_discovery_result

    def different_original_root(tool_name, result):
        admitted = original(tool_name, result)
        if tool_name != "list_models":
            return admitted
        # Preserve selected evidence while deliberately replacing the root
        # it superseded. The planner must never revisit that root on egress
        # or infer completeness from it.
        data = {
            "models": ["anthropic/original-root-canary"],
            "count": 2 if admitted_complete else 1,
            "truncated": admitted_complete,
        }
        return replace(admitted, result=replace(result, data=data))

    monkeypatch.setattr(pipeline_planner, "admit_discovery_result", different_original_root)
    completion = _ScriptedCompletion(
        _response(("list_models", {"provider": "anthropic", "limit": 2 if admitted_complete else 1})),
        _response(("list_models", {"provider": "anthropic", "limit": 10})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, information_aware=True)

    final_tool = [json.loads(message["content"]) for message in completion.requests[2]["messages"] if message["role"] == "tool"][-1]
    if admitted_complete:
        assert final_tool["error_code"] == "DISCOVERY_NO_GAIN"
    else:
        assert final_tool["success"] is True
        assert final_tool["data"]["models"] == ["anthropic/model-a", "anthropic/model-b"]
