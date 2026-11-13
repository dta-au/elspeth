"""Request-scoped diff baselines through the real freeform dispatch loop."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web.composer._helpers import (
    _clean_advisor_checkpoint,
    _composer_service_with_session,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)
from tests.unit.web.composer.test_tools import _empty_state, _stage1_valid_preview_state, execute_tool
from tests.unit.web.sessions.test_composer_proposals import _session_operation_context


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
@pytest.mark.parametrize("strict_start", [False, True])
async def test_diff_uses_request_start_and_resets_for_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later_round: bool, explicit_approval: bool, strict_start: bool
) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    monkeypatch.setattr(
        service._preflight,
        "runtime_preflight",
        lambda candidate, *_args, **_kwargs: ValidationResult(
            is_valid=strict_start or bool(candidate.metadata.description),
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
    assert payloads[1]["data"]["baseline_kind"] == ("last_validated" if strict_start and not explicit_approval else "request_start")
    assert payloads[1]["data"]["metadata_changed"] is (not explicit_approval)
    assert payloads[1]["data"]["total_changes"] == (0 if explicit_approval else 1)
    # Held (staged, unapplied) edits are invisible to the diff: say so truthfully, and only then.
    assert "post_call_hints" not in payloads[0]
    if explicit_approval:
        assert len(payloads[1]["post_call_hints"]) == 1
        assert "not applied" in payloads[1]["post_call_hints"][0]
        assert "pending approval" in payloads[1]["post_call_hints"][0]
    else:
        assert "post_call_hints" not in payloads[1]

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


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True], ids=["request-start", "last-accepted"])
@pytest.mark.parametrize("later_round", [False, True], ids=["same-batch", "later-round"])
async def test_diff_reports_baseline_and_preserves_accepted_state_across_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted: bool, later_round: bool
) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Diff baseline", "local")).id
    session_id = str(sid)
    state = CompositionState(
        nodes=(),
        edges=(),
        metadata=PipelineMetadata(),
        version=1,
        source=SourceSpec(
            plugin="csv",
            on_success="main",
            on_validation_failure="discard",
            options={"path": str(tmp_path / "blobs" / session_id / "input.csv"), "schema": {"mode": "observed"}},
        ),
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
    )
    monkeypatch.setattr(
        service._preflight,
        "runtime_preflight",
        lambda *_a, **_kw: ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        ),
    )
    current_state_id = None
    if accepted:
        # Two real accepted records prove selection of the newest, not the first.
        for description in ("Older acceptance", "Newest acceptance"):
            state = state.with_metadata({"description": description})
            async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
                proposal = await sessions.create_composition_proposal(
                    session_id=sid,
                    tool_call_id=description,
                    tool_name="set_metadata",
                    summary=description,
                    rationale="User requested this description",
                    affects=("metadata",),
                    arguments_json={"patch": {"description": description}},
                    arguments_redacted_json={"patch": {"description": description}},
                    base_state_id=current_state_id,
                    actor="test-user",
                    session_operation_context=context,
                )
            raw = state.to_dict()
            async with _session_operation_context(sessions, sid, SessionOperationKind.PROPOSAL) as context:
                committed = await sessions.accept_composition_proposal(
                    session_id=sid,
                    proposal_id=proposal.id,
                    expected_current_state_id=current_state_id,
                    state=CompositionStateData(
                        sources=raw["sources"],
                        nodes=raw["nodes"],
                        edges=raw["edges"],
                        outputs=raw["outputs"],
                        metadata_=raw["metadata"],
                        is_valid=True,
                    ),
                    actor="test-user",
                    session_operation_context=context,
                )
            assert committed.committed_state_id is not None
            current_state_id = committed.committed_state_id
            state = state_from_record(await sessions.get_state(current_state_id))
    before = _call("diff_pipeline", {}, "diff_before_edits")
    edits = (
        _call("set_metadata", {"patch": {"description": "First edit after acceptance"}}, "edit_one"),
        _call("patch_output_options", {"sink_name": "main", "patch": {"delimiter": ";"}}, "edit_two"),
    )
    diff = _call("diff_pipeline", {}, "diff_edits")
    responses = [_response(before, *edits), _response(diff)] if later_round else [_response(before, *edits, diff)]
    responses.append(_admit_composer_llm_completion(_make_llm_response(content="Edits inspected.")))
    with (
        patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider,
        patch.object(sessions, "get_last_validated_state", wraps=sessions.get_last_validated_state) as baseline_read,
    ):
        provider.side_effect = responses
        result = await service.compose(
            "Make both edits and inspect them.",
            [],
            state,
            session_id=session_id,
            current_state_id=str(current_state_id) if current_state_id else None,
        )
    assert baseline_read.await_count == 1
    initial_payload, payload = _diff_payloads(result)
    assert initial_payload["success"], initial_payload
    assert initial_payload["data"]["total_changes"] == (1 if accepted else 0)
    assert payload["success"], payload
    assert "baseline_kind" in payload["data"]
    assert payload["data"]["baseline_kind"] == "last_validated"
    assert payload["data"]["from_version"] == state.version
    assert payload["data"]["metadata_changed"]
    edit_result = json.loads(
        next(
            invocation.result_canonical
            for invocation in result.tool_invocations
            if invocation.tool_name == "patch_output_options" and invocation.result_canonical is not None
        )
    )
    assert edit_result["success"], json.dumps(edit_result)
    assert payload["data"]["outputs"]["modified"] == ["main"]
    assert payload["data"]["total_changes"] == 2
    proposals = await sessions.list_composition_proposals(sid)
    assert len(proposals) == (2 if accepted else 0), "Auto-commit edits must not invent accepted proposals"
    current = await sessions.get_current_state(sid)
    assert current is not None
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = [
            _response(_call("diff_pipeline", {}, "diff_next")),
            _admit_composer_llm_completion(_make_llm_response(content="Diff inspected.")),
        ]
        next_result = await service.compose("What changed?", [], result.state, session_id=session_id, current_state_id=str(current.id))
    next_payload = _diff_payloads(next_result)[0]
    assert next_payload["success"], next_payload
    assert next_payload["data"]["baseline_kind"] == ("last_validated" if accepted else "request_start")
    assert next_payload["data"]["from_version"] == (state.version if accepted else result.state.version)
    assert next_payload["data"]["total_changes"] == (2 if accepted else 0)


@pytest.mark.parametrize("valid", [False, True])
def test_missing_baseline_does_not_change_graph_validity(valid: bool) -> None:
    state = _stage1_valid_preview_state() if valid else _empty_state()
    assert state.validate().is_valid is valid
    result = execute_tool("diff_pipeline", {}, state, _mock_catalog())
    assert not result.success
    assert result.validation.is_valid is valid
    assert result.validation.errors[0].error_code == "diff_baseline_unavailable"
    assert result.validation.errors[1:] == state.validate().errors


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_accepted_baseline_credential_refusal_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    valid: bool,
) -> None:
    from elspeth.web.composer.protocol import ComposerPluginCrashError

    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Refused baseline", "local")).id
    session_id = str(sid)
    clean = _stage1_valid_preview_state() if valid else _empty_state()
    marker = "sk-" + "a" * 24
    poisoned = replace(
        _stage1_valid_preview_state(),
        outputs=(replace(_stage1_valid_preview_state().outputs[0], name=marker),),
        metadata=PipelineMetadata(description="refused-baseline-description"),
    )

    async def commit(state: CompositionState, label: str) -> UUID:
        head = await sessions.get_current_state(sid)
        base_id = head.id if head else None
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            proposal = await sessions.create_composition_proposal(
                session_id=sid,
                tool_call_id=label,
                tool_name="set_metadata",
                summary=label,
                rationale="Baseline test",
                affects=("metadata",),
                arguments_json={"patch": {}},
                arguments_redacted_json={"patch": {}},
                base_state_id=base_id,
                actor="test-user",
                session_operation_context=context,
            )
        raw = state.to_dict()
        async with _session_operation_context(sessions, sid, SessionOperationKind.PROPOSAL) as context:
            accepted = await sessions.accept_composition_proposal(
                session_id=sid,
                proposal_id=proposal.id,
                expected_current_state_id=base_id,
                state=CompositionStateData(
                    sources=raw["sources"],
                    nodes=raw["nodes"],
                    edges=raw["edges"],
                    outputs=raw["outputs"],
                    metadata_=raw["metadata"],
                    is_valid=True,
                ),
                actor="test-user",
                session_operation_context=context,
            )
        assert accepted.committed_state_id is not None
        return accepted.committed_state_id

    async def save_working_state(state: CompositionState) -> tuple[CompositionState, UUID]:
        raw = state.to_dict()
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            record = await sessions.save_composition_state(
                sid,
                CompositionStateData(
                    sources=raw["sources"],
                    nodes=raw["nodes"],
                    edges=raw["edges"],
                    outputs=raw["outputs"],
                    metadata_=raw["metadata"],
                    is_valid=False,
                    composer_meta={"validation_lane": "strict"},
                ),
                provenance="session_seed",
                session_operation_context=context,
            )
        return state_from_record(record), record.id

    await commit(clean, "older-clean")
    await commit(poisoned, "refused-acceptance")
    clean, current_id = await save_working_state(clean)
    events: list[ComposerProgressEvent] = []

    async def progress(event: ComposerProgressEvent) -> None:
        events.append(event)

    for request in range(3):
        if request == 2:
            current_id = await commit(clean.with_metadata({"description": "Clean acceptance"}), "clean-acceptance")
            clean, current_id = await save_working_state(state_from_record(await sessions.get_state(current_id)))
        events.clear()
        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
            provider.side_effect = [
                _response(_call("diff_pipeline", {}, f"diff_{request}_first"), _call("get_pipeline_state", {}, f"state_{request}_first")),
                _response(_call("diff_pipeline", {}, f"diff_{request}_later"), _call("get_pipeline_state", {}, f"state_{request}_later")),
                _admit_composer_llm_completion(_make_llm_response(content="Inspection complete.")),
            ]
            try:
                result = await service.compose(
                    "Inspect changes.", [], clean, session_id=session_id, current_state_id=str(current_id), progress=progress
                )
            except ComposerPluginCrashError as exc:
                raise AssertionError("A refused comparison baseline must fail the tool call, not crash the turn") from exc
            assert provider.await_count == 3
            captured = capsys.readouterr()
            exposed = repr(provider.call_args_list) + repr(result) + repr(events) + caplog.text + captured.out + captured.err
        assert marker not in exposed
        assert "refused-baseline-description" not in exposed
        payloads = _diff_payloads(result)
        assert len(payloads) == 2
        for payload in payloads:
            assert payload["success"] is (request == 2)
            assert payload["validation"]["is_valid"] is valid
            if request < 2:
                assert payload["validation"]["errors"][0]["error_code"] == "diff_baseline_unavailable"
                assert "refused by the credential policy" in payload["validation"]["errors"][0]["message"]
                assert "data" not in payload or not payload["data"]
            else:
                assert payload["data"]["baseline_kind"] == "last_validated"
                assert payload["data"]["total_changes"] == 0
        inspections = [
            json.loads(inv.result_canonical)
            for inv in result.tool_invocations
            if inv.tool_name == "get_pipeline_state" and inv.result_canonical is not None
        ]
        assert len(inspections) == 2
        assert all(item["success"] for item in inspections)
        refusals = [event for event in events if event.reason == "admission_refused"]
        assert len(refusals) == (1 if request < 2 else 0)
        if refusals:
            assert refusals[0].phase == "using_tools"
        clean = result.state
        head = await sessions.get_current_state(sid)
        assert head is not None
        current_id = head.id


@pytest.mark.asyncio
async def test_planner_auto_commit_without_previous_valid_state_uses_request_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.protocol import PipelineCommitIntent
    from elspeth.web.composer.service import ComposerServiceImpl
    from tests.unit.web.sessions.test_run_composer_turn import (
        _create_running_pipeline_route_proposal,
        _make_composer_mock,
        _prepare_canonical_pipeline_route_proposal,
        _run,
        _running_job,
    )

    app, sessions, _pipeline, sid, creation, _endpoint = await _prepare_canonical_pipeline_route_proposal(
        tmp_path, monkeypatch, tool_call_id="baseline-planner-build"
    )
    planner = _make_composer_mock()

    async def planned(*args: Any, **kwargs: Any) -> ComposerResult:
        row = await _create_running_pipeline_route_proposal(sessions, creation, kwargs)
        assert row.pipeline_metadata is not None
        return ComposerResult(
            message="Pipeline prepared.",
            state=_empty_state(),
            pipeline_commit_intent=PipelineCommitIntent(proposal_id=row.id, draft_hash=row.pipeline_metadata.draft_hash),
        )

    planner.compose.side_effect = planned
    app.state.composer_service = planner
    async with _running_job(app, sessions, sid, kind="compose_message", content="Build the pipeline.") as job:
        completed = await _run(app, job)
    assert completed.status == "completed"
    proposals = await sessions.list_composition_proposals(sid)
    assert [proposal.status for proposal in proposals] == ["committed"]
    current = await sessions.get_current_state(sid)
    assert current is not None and proposals[0].committed_state_id == current.id

    composer = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service,
        settings=_make_settings(data_dir=tmp_path),
        sessions_service=sessions,
        session_engine=app.state.session_engine,
    )
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    with patch.object(composer._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = [
            _response(_call("diff_pipeline", {}, "diff_planner_build")),
            _admit_composer_llm_completion(_make_llm_response(content="No changes since the build.")),
        ]
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            result = await composer.compose(
                "What changed?",
                [],
                state_from_record(current),
                session_id=str(sid),
                current_state_id=str(current.id),
                user_id="alice",
                session_operation_context=context,
            )
    payload = _diff_payloads(result)[0]
    assert payload["success"], payload
    assert payload["data"]["baseline_kind"] == "request_start"
    assert payload["data"]["from_version"] == current.version
    assert payload["data"]["total_changes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_in_response", [False, True])
async def test_baseline_lookup_follows_response_scan_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_in_response: bool
) -> None:
    from elspeth.web.composer.protocol import ComposerPluginCrashError
    from elspeth.web.credential_guard import CredentialMaterialRefused

    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    lookup = AsyncMock(spec=sessions.get_last_validated_state, side_effect=ValueError("baseline lookup failed"))
    monkeypatch.setattr(sessions, "get_last_validated_state", lookup)
    completion = _admit_composer_llm_completion(
        _make_llm_response(
            content="Inspecting changes.",
            tool_calls=[_call("diff_pipeline", {}, "diff_lookup_failure")],
        )
    )
    if credential_in_response:
        # Exercise the batch guard for an internal already-admitted completion.
        completion = replace(completion, message=replace(completion.message, content="sk-" + "a" * 24))
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.return_value = completion
        if credential_in_response:
            with pytest.raises(CredentialMaterialRefused):
                await service.compose("Inspect changes.", [], _stage1_valid_preview_state(), session_id=session_id)
            lookup.assert_not_awaited()
        else:
            with pytest.raises(ComposerPluginCrashError) as caught:
                await service.compose("Inspect changes.", [], _stage1_valid_preview_state(), session_id=session_id)
            assert isinstance(caught.value.original_exc, ValueError)
            assert caught.value.failed_turn is not None
            assert lookup.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["strict", "authoring_only", "unmarked", None, "unknown", 7])
@pytest.mark.parametrize("invalid_rows", [0, 55])
@pytest.mark.parametrize("edit_first", [False, True])
async def test_diff_selects_previous_validated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: object, invalid_rows: int, edit_first: bool
) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Validated baseline", "local")).id
    session_id = str(sid)
    state = _stage1_valid_preview_state()
    monkeypatch.setattr(
        service._preflight,
        "runtime_preflight",
        lambda *_a, **_kw: ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        ),
    )
    records = []
    for index, lane in enumerate(("unmarked", marker, *(["strict"] * invalid_rows), "strict")):
        raw = state.with_metadata({"description": str(index)}).to_dict()
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            records.append(
                await sessions.save_composition_state(
                    sid,
                    CompositionStateData(
                        sources=raw["sources"],
                        nodes=raw["nodes"],
                        edges=raw["edges"],
                        outputs=raw["outputs"],
                        metadata_=raw["metadata"],
                        is_valid=index < 2 or index == invalid_rows + 2,
                        composer_meta={} if lane == "unmarked" else {"validation_lane": lane},
                    ),
                    provenance="session_seed",
                    session_operation_context=context,
                )
            )
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = [
            _response(
                *([_call("set_metadata", {"patch": {"description": "edited"}}, "edit_validated")] if edit_first else []),
                _call("diff_pipeline", {}, "validated_diff"),
            ),
            _admit_composer_llm_completion(_make_llm_response(content="Compared.")),
        ]
        result = await service.compose(
            "What changed?", [], state_from_record(records[-1]), session_id=session_id, current_state_id=str(records[-1].id)
        )
    payload = _diff_payloads(result)[0]
    assert payload["data"]["baseline_kind"] == "last_validated"
    assert payload["data"]["from_version"] == records[-1 if edit_first else (1 if marker in ("unmarked", "strict") else 0)].version
    assert payload["data"]["metadata_changed"]


@pytest.mark.asyncio
async def test_baseline_guard_fault_is_an_audited_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.protocol import ComposerPluginCrashError

    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, _ = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Guard fault", "local")).id
    state = _stage1_valid_preview_state()
    raw = state.to_dict()
    records = []
    for valid in (True, False):
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            records.append(
                await sessions.save_composition_state(
                    sid,
                    CompositionStateData(
                        sources=raw["sources"],
                        nodes=raw["nodes"],
                        edges=raw["edges"],
                        outputs=raw["outputs"],
                        metadata_=raw["metadata"],
                        is_valid=valid,
                    ),
                    provenance="session_seed",
                    session_operation_context=context,
                )
            )
    current = records[-1]
    with (
        patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider,
        patch(
            "elspeth.web.composer.tool_batch.require_no_credential_material_in_state",
            autospec=True,
            side_effect=RuntimeError("guard defect"),
        ),
    ):
        provider.side_effect = [
            _response(_call("diff_pipeline", {}, "guard_fault"), _call("get_pipeline_state", {}, "guard_state")),
            _admit_composer_llm_completion(_make_llm_response(content="Compared.")),
        ]
        with pytest.raises(ComposerPluginCrashError) as caught:
            await service.compose("Compare.", [], state_from_record(current), session_id=str(sid), current_state_id=str(current.id))
    assert isinstance(caught.value.original_exc, RuntimeError)
    assert str(caught.value.original_exc) == "guard defect"
    assert caught.value.failed_turn is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["invalid", "never", "revert"])
async def test_diff_repair_and_revert_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: str) -> None:
    from uuid import uuid4

    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, _ = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Repair history", "local")).id
    records = []
    for index in range(3):
        state = _stage1_valid_preview_state()
        if history != "revert" and index > 0:
            state = replace(state, outputs=())
        raw = state.with_metadata({"description": str(index)}).to_dict()
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            records.append(
                await sessions.save_composition_state(
                    sid,
                    CompositionStateData(
                        sources=raw["sources"],
                        nodes=raw["nodes"],
                        edges=raw["edges"],
                        outputs=raw["outputs"],
                        metadata_=raw["metadata"],
                        is_valid=history != "never" and (history == "revert" or index == 0),
                        composer_meta={"validation_lane": "strict"},
                    ),
                    provenance="session_seed",
                    session_operation_context=context,
                )
            )
    current = records[-1]
    expected = records[0] if history == "invalid" else current
    if history == "revert":
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            receipt = await sessions.reserve_operation_receipt(
                session_id=sid,
                operation_id=str(uuid4()),
                kind="state_revert",
                request_hash="a" * 64,
                actor="test-user",
                lease_seconds=30,
                session_operation_context=context,
            )
            current = await sessions.revert_state_for_operation_receipt(
                receipt.fence,
                state_id=records[0].id,
                expected_current_state_id=current.id,
                expected_current_state_version=current.version,
                actor="test-user",
                response_hash_factory=lambda _state: "b" * 64,
                session_operation_context=context,
            )
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = [
            _response(_call("diff_pipeline", {}, "repair_diff")),
            _admit_composer_llm_completion(_make_llm_response(content="Compared.")),
        ]
        result = await service.compose("Compare.", [], state_from_record(current), session_id=str(sid), current_state_id=str(current.id))
    payload = _diff_payloads(result)[0]
    assert payload["success"]
    assert payload["data"]["baseline_kind"] == ("request_start" if history == "never" else "last_validated")
    assert payload["data"]["from_version"] == expected.version
    assert payload["data"]["metadata_changed"] is (history != "never")


@pytest.mark.asyncio
async def test_validated_history_read_is_session_scoped_and_bounded(tmp_path: Path) -> None:
    service, _ = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Bounded history", "local")).id
    empty_sid = (await sessions.create_session("test-user", "Empty history", "local")).id
    raw = _stage1_valid_preview_state().to_dict()
    async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
        first = await sessions.save_composition_state(
            sid,
            CompositionStateData(
                sources=raw["sources"],
                nodes=raw["nodes"],
                edges=raw["edges"],
                outputs=raw["outputs"],
                metadata_=raw["metadata"],
                is_valid=True,
                composer_meta={"validation_lane": "strict"},
            ),
            provenance="session_seed",
            session_operation_context=context,
        )
        found = await sessions.get_last_validated_state(sid, through_version=102, exclude_version=102)
        assert found is not None and found.id == first.id
        assert await sessions.get_last_validated_state(empty_sid, through_version=102, exclude_version=102) is None
        for _ in range(100):
            await sessions.save_composition_state(
                sid,
                CompositionStateData(
                    sources=raw["sources"],
                    nodes=raw["nodes"],
                    edges=raw["edges"],
                    outputs=raw["outputs"],
                    metadata_=raw["metadata"],
                    is_valid=True,
                    composer_meta={"validation_lane": "authoring_only"},
                ),
                provenance="session_seed",
                session_operation_context=context,
            )
    found = await sessions.get_last_validated_state(sid, through_version=102, exclude_version=102)
    assert found is not None and found.id == first.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("head_valid", "head_lane"),
    [(False, "strict"), (True, "authoring_only")],
    ids=["invalid-head", "authoring-only-head"],
)
async def test_edit_then_diff_skips_ineligible_request_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head_valid: bool, head_lane: str
) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, _ = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Ineligible head", "local")).id
    monkeypatch.setattr(
        service._preflight,
        "runtime_preflight",
        lambda *_a, **_kw: ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        ),
    )
    state = _stage1_valid_preview_state()
    records = []
    for index, (valid, lane) in enumerate(((True, "strict"), (head_valid, head_lane))):
        raw = state.with_metadata({"description": str(index)}).to_dict()
        async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
            records.append(
                await sessions.save_composition_state(
                    sid,
                    CompositionStateData(
                        sources=raw["sources"],
                        nodes=raw["nodes"],
                        edges=raw["edges"],
                        outputs=raw["outputs"],
                        metadata_=raw["metadata"],
                        is_valid=valid,
                        composer_meta={"validation_lane": lane},
                    ),
                    provenance="session_seed",
                    session_operation_context=context,
                )
            )
    current = records[-1]
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = [
            _response(
                _call("set_metadata", {"patch": {"description": "edited"}}, "edit"),
                _call("diff_pipeline", {}, "diff_after_edit"),
            ),
            _admit_composer_llm_completion(_make_llm_response(content="Compared.")),
        ]
        result = await service.compose(
            "Edit and compare.", [], state_from_record(current), session_id=str(sid), current_state_id=str(current.id)
        )
    payload = _diff_payloads(result)[0]
    assert payload["success"]
    assert payload["data"]["baseline_kind"] == "last_validated"
    assert payload["data"]["from_version"] == records[0].version


@pytest.mark.asyncio
async def test_diff_finds_strict_valid_row_after_full_authoring_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, _ = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    sid = (await sessions.create_session("test-user", "Long authoring history", "local")).id
    state = _stage1_valid_preview_state()
    raw = state.to_dict()
    async with _session_operation_context(sessions, sid, SessionOperationKind.COMPOSE) as context:
        first = None
        current = None
        for index in range(102):
            current = await sessions.save_composition_state(
                sid,
                CompositionStateData(
                    sources=raw["sources"],
                    nodes=raw["nodes"],
                    edges=raw["edges"],
                    outputs=raw["outputs"],
                    metadata_=raw["metadata"],
                    is_valid=index < 101,
                    composer_meta={"validation_lane": "strict" if index in (0, 101) else "authoring_only"},
                ),
                provenance="session_seed",
                session_operation_context=context,
            )
            if index == 0:
                first = current
    assert first is not None and current is not None
    with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
        provider.side_effect = [
            _response(_call("diff_pipeline", {}, "long_history_diff")),
            _admit_composer_llm_completion(_make_llm_response(content="Compared.")),
        ]
        result = await service.compose("Compare.", [], state_from_record(current), session_id=str(sid), current_state_id=str(current.id))
    payload = _diff_payloads(result)[0]
    assert payload["success"]
    assert payload["data"]["baseline_kind"] == "last_validated"
    assert payload["data"]["from_version"] == first.version
