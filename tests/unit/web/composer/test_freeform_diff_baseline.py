"""Request-scoped diff baselines through the real freeform dispatch loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from tests.unit.web.composer._helpers import (
    _clean_advisor_checkpoint,
    _composer_service_with_session,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)


def _call(name: str, arguments: dict[str, Any], identifier: str) -> dict[str, Any]:
    return {"id": identifier, "name": name, "arguments": arguments}


def _response(*calls: dict[str, Any]) -> _AdmittedLLMCompletion:
    return _admit_composer_llm_completion(_make_llm_response(tool_calls=list(calls)))


def _diff_payloads(result: ComposerResult) -> list[dict[str, Any]]:
    return [
        json.loads(invocation.result_canonical)
        for invocation in result.tool_invocations
        if invocation.tool_name == "diff_pipeline" and invocation.result_canonical is not None
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("later_round", [False, True], ids=["same-batch", "later-round"])
@pytest.mark.parametrize("explicit_approval", [False, True], ids=["auto-commit", "explicit-approval"])
async def test_diff_uses_request_start_and_resets_for_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later_round: bool, explicit_approval: bool
) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    monkeypatch.setattr(
        service._preflight,
        "runtime_preflight",
        lambda *_args, **_kwargs: ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        ),
    )
    if explicit_approval:
        await service._require_sessions_service().update_composer_preferences(
            UUID(session_id), trust_mode="explicit_approve", density_default="high", actor="test-user"
        )
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="main",
            options={"path": str(tmp_path / "blobs" / session_id / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="csv",
                on_write_failure="discard",
                options={
                    "path": str(tmp_path / "outputs" / session_id / "output.csv"),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )
    start = _call("diff_pipeline", {}, "diff_start")
    mutation = _call("set_metadata", {"patch": {"description": "Provider-authored edit"}}, "edit")
    after = _call("diff_pipeline", {}, "diff_after")
    responses = [_response(start, mutation), _response(after)] if later_round else [_response(start, mutation, after)]
    responses.append(_admit_composer_llm_completion(_make_llm_response(content="Changes inspected.")))
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = responses
        result = await service.compose("Update the description and inspect the diff.", [], state, session_id=session_id)
    payloads = _diff_payloads(result)
    assert len(payloads) == 2
    assert all(payload["success"] for payload in payloads), payloads
    assert payloads[0]["data"]["from_version"] == state.version
    assert payloads[0]["data"]["total_changes"] == 0
    assert payloads[1]["data"]["from_version"] == state.version
    assert payloads[1]["data"]["metadata_changed"] is (not explicit_approval)
    assert payloads[1]["data"]["total_changes"] == (0 if explicit_approval else 1)

    current_record = await service._require_sessions_service().get_current_state(UUID(session_id))
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = [
            _response(_call("diff_pipeline", {}, "diff_next_request")),
            _admit_composer_llm_completion(_make_llm_response(content="No changes this request.")),
        ]
        next_result = await service.compose(
            "Inspect this request's diff.",
            [],
            result.state,
            session_id=session_id,
            current_state_id=str(current_record.id) if current_record is not None else None,
        )
    next_payloads = _diff_payloads(next_result)
    assert len(next_payloads) == 1
    assert next_payloads[0]["success"]
    assert next_payloads[0]["data"]["from_version"] == result.state.version
    assert next_payloads[0]["data"]["total_changes"] == 0
