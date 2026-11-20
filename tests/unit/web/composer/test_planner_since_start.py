"""Turn-start inspection through the real planner discovery call."""

import json

import pytest

from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PlannerDeclined
from tests.unit.web.composer.test_pipeline_planner import _empty_state, _plan, _response_with_call_id, _ScriptedCompletion, _text_response


@pytest.mark.asyncio
@pytest.mark.parametrize("has_evidence", [False, True])
async def test_planner_uses_turn_initial_state_for_since_start(tmp_path, tool_context, has_evidence: bool) -> None:
    initial = _empty_state()
    current = initial.with_metadata({"description": "changed before planner"})
    completion = _ScriptedCompletion(
        _response_with_call_id("read", "get_pipeline_state", {"since_start": True}),
        _text_response("Done inspecting."),
    )
    recorder = BufferingRecorder()
    with pytest.raises(PlannerDeclined):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            current_state=current,
            turn_evidence_initial_state=initial if has_evidence else None,
        )
    payload = json.loads(recorder.invocations[0].result_canonical)
    assert "success" in payload, payload
    if has_evidence:
        assert payload["success"], payload
        assert payload["data"]["from_version"] == initial.version
        assert {"operation": "set", "path": ["metadata", "description"], "value": "changed before planner"} in payload["data"]["changes"]
    else:
        assert not payload["success"], payload
        assert payload["validation"]["errors"][0]["error_code"] == "diff_baseline_unavailable"
