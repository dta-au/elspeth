"""Review preflight failures retain their infrastructure/deadline identity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.errors import FailedTurnMetadata
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, finish_success
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.service import ComposerServiceImpl, _admit_composer_llm_completion, _AdvisorCheckpointComposeDeadlineExpired
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.execution.test_validation_complaint_triage import complaint_triage_state

from .conftest import _fake_llm_response
from .test_preview_policy_snapshot import _service


@pytest.mark.anyio
@pytest.mark.parametrize("deadline_expired", [False, True])
async def test_review_preflight_failure_is_not_a_plugin_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deadline_expired: bool,
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    service = _service(tmp_path, snapshot)
    service._sessions_service = composer_service_with_real_sessions._sessions_service
    state = complaint_triage_state(pending=True)
    recorder = BufferingRecorder()
    prior = finish_success(
        begin_dispatch("previous", "set_metadata", {"patch": {"name": "Complaint triage"}}, version_before=0, actor="test"),
        result_payload={"success": True},
        version_after=state.version,
    )
    recorder.record(prior)
    original = RuntimeError("preflight worker failed")
    failed_turn = FailedTurnMetadata(assistant_message_id=str(uuid4()), tool_calls_attempted=2)
    failure = (
        _AdvisorCheckpointComposeDeadlineExpired()
        if deadline_expired
        else ComposerRuntimePreflightError(original_exc=original, partial_state=state, failed_turn=failed_turn)
    )
    observed_deadlines = []

    async def fail_preflight(*_args: Any, **kwargs: Any) -> None:
        observed_deadlines.append(kwargs["deadline"])
        raise failure

    monkeypatch.setattr(service, "_cached_runtime_preflight", fail_preflight)
    completion = _admit_composer_llm_completion(
        _fake_llm_response(
            tool_calls=(
                {
                    "id": "review",
                    "name": "request_interpretation_review",
                    "arguments": {
                        "affected_node_id": "classify_category",
                        "kind": "vague_term",
                        "user_term": "category",
                        "llm_draft": "billing, outage, or other",
                    },
                },
            )
        )
    )
    deadline = asyncio.get_running_loop().time() + 60
    with pytest.raises(type(failure)) as raised:
        await service._dispatch_tool_batch(
            call_model=_CallModelOutcome(completion=completion),
            state=state,
            last_validation=None,
            last_runtime_preflight=None,
            llm_messages=[],
            recorder=recorder,
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache={},
            session_id=result_session_id,
            user_id=None,
            user_message_id=None,
            user_message_content=None,
            current_state_id=str(uuid4()),
            actor="test",
            initial_version=0,
            deadline=deadline,
            progress=None,
            session_scope="review-preflight-failure",
            advisor_calls_used=0,
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=snapshot,
            policy_catalog=PolicyCatalogView(catalog, snapshot, service._operator_profile_registry),
        )

    assert observed_deadlines == [deadline]
    assert recorder.invocations == (prior,)
    if isinstance(raised.value, ComposerRuntimePreflightError):
        assert raised.value.original_exc is original
        assert raised.value.partial_state is state
        assert raised.value.failed_turn is failed_turn
        assert raised.value.tool_invocations == (prior,)
        assert raised.value.llm_calls == recorder.llm_calls
    else:
        assert raised.value is failure
