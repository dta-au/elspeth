"""Public planner admits and snapshots restricted policy context before effects."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.guided.planning import guided_redacted_current_state_context
from elspeth.web.composer.pipeline_proposal import PlannerSurface
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolContext
from tests.unit.web.composer.test_pipeline_planner import _lifecycle, _pipeline, _plan, _Response, _response, _ScriptedCompletion


def _current_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={"path": "private.csv"}, on_validation_failure="discard"),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=4,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "extra"])
async def test_public_plan_rejects_malformed_context_before_lifecycle_and_provider(
    tmp_path: Path, tool_context: ToolContext, corruption: str
) -> None:
    state = _current_state()
    context = guided_redacted_current_state_context(state)
    if corruption == "missing":
        del context["sources"]
    else:
        context["private_extra"] = "must not escape"
    events: list[str] = []
    completion = _ScriptedCompletion(_response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})))
    with pytest.raises(FrameworkBugError):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            current_state=state,
            provider_current_state=context,
            surface=PlannerSurface.GUIDED_STAGED,
            lifecycle=_lifecycle(events),
        )
    assert events == []
    assert completion.requests == []


@pytest.mark.asyncio
async def test_public_plan_freezes_original_nested_context_before_first_provider_call(tmp_path: Path, tool_context: ToolContext) -> None:
    state = _current_state()
    context = guided_redacted_current_state_context(state)
    context["version"] = 37
    expected = deepcopy(context)
    source_rows = context["sources"]
    assert isinstance(source_rows, list)
    source = source_rows[0]
    assert isinstance(source, dict)
    option_keys = source["option_keys"]
    assert isinstance(option_keys, list)

    class MutatingCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> _Response:
            if not self.requests:
                source["plugin"] = "PRIVATE-MUTATED-PLUGIN"
                option_keys.append("PRIVATE-MUTATED-KEY")
                context["version"] = 999
            return await super().__call__(**kwargs)

    completion = MutatingCompletion(
        _response(("get_pipeline_state", {})),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        current_state=state,
        provider_current_state=context,
        surface=PlannerSurface.GUIDED_STAGED,
    )
    assert len(completion.requests) == 2
    initial = json.loads(completion.requests[0]["messages"][1]["content"])
    assert initial["current_state"] == expected
    tool_payload = next(json.loads(message["content"]) for message in completion.requests[1]["messages"] if message["role"] == "tool")
    assert tool_payload["data"] == expected
    assert tool_payload["version"] == state.version == 4
    assert tool_payload["data"]["version"] == 37
    assert context["version"] == 999
    assert source["plugin"] == "PRIVATE-MUTATED-PLUGIN"
    assert "PRIVATE-MUTATED-KEY" in option_keys
