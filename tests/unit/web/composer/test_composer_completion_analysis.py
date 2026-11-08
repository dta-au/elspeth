"""Completion consumes exact shared graph summaries without retaining live verdicts."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from elspeth.web.composer import composition_completion
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdvisorReviewState
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.composition_completion import (
    _source_proof_unavailable_validation,
    _TerminalNoToolAdvisorGateOutcome,
)
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from tests.fixtures.composer_session_cache import make_small_composer_state
from tests.unit.web.composer._helpers import _make_settings, _mock_catalog


def _runtime_result(*, valid: bool) -> ValidationResult:
    return ValidationResult(
        is_valid=valid,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=valid,
            execution_ready=valid,
            completion_ready=valid,
            blockers=[],
        ),
    )


def _owner(tmp_path: Path) -> ComposerServiceImpl:
    return ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))


def test_source_proof_projection_uses_supplied_exact_raw_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    state = replace(make_small_composer_state(), nodes=())
    authored = state.validate()
    assert not authored.is_valid

    def forbidden_validation(_state: CompositionState) -> ValidationSummary:
        raise AssertionError("Precomputed source-proof projection repeated graph validation")

    monkeypatch.setattr(CompositionState, "validate", forbidden_validation)
    result = _source_proof_unavailable_validation(
        state,
        MalformedSourceProofClaim("primary", "The source blob identity is malformed."),
        None,
        authored_validation=authored,
    )
    assert result.errors[0].error_code == "source_blob_identity_invalid"
    assert [(error.component_id, error.message, error.error_code) for error in result.errors[1:]] == [
        (error.component, error.message, error.error_code) for error in authored.errors
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_analysis", [False, True])
@pytest.mark.parametrize("has_runtime_prior", [False, True])
async def test_source_proof_fallback_is_async_and_prior_runtime_remains_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_analysis: bool,
    has_runtime_prior: bool,
) -> None:
    owner = _owner(tmp_path)
    completion = owner._completion
    state = make_small_composer_state()
    registry = ComposerSessionCacheRegistry()
    loop_thread = threading.get_ident()
    validation_threads: list[int] = []
    original_validate = CompositionState.validate

    def tracked_validate(current: CompositionState) -> ValidationSummary:
        validation_threads.append(threading.get_ident())
        return original_validate(current)

    monkeypatch.setattr(CompositionState, "validate", tracked_validate)
    orphan = AsyncMock(spec=completion._surface_pt_and_gate_orphans_or_none, return_value=None)
    gate = AsyncMock(
        spec=completion._evaluate_terminal_no_tool_advisor_gate,
        return_value=_TerminalNoToolAdvisorGateOutcome(action="fall_through"),
    )
    monkeypatch.setattr(completion, "_surface_pt_and_gate_orphans_or_none", orphan)
    monkeypatch.setattr(completion, "_evaluate_terminal_no_tool_advisor_gate", gate)
    evidence = TurnEvidence(
        session_id=None,
        session_operation_context=None,
        request_id="source-proof-cache",
        request="Inspect the source.",
        initial_state_id=None,
        initial_state=state,
    )
    prior = _runtime_result(valid=True) if has_runtime_prior else None
    with registry.borrow(SessionCacheKey("principal", "session")) as lease:
        analysis = ComposerAnalysis(lease.cache) if shared_analysis else None
        for _ in range(2):
            outcome = await completion._terminate_source_proof(
                proof=MalformedSourceProofClaim("primary", "The source blob identity is malformed."),
                state=state,
                assistant_message=_AdmittedAssistantMessage(content="The source needs attention."),
                turn_evidence=evidence,
                message="Inspect the source.",
                session_id=None,
                current_state_id=None,
                session_operation_context=None,
                user_id=None,
                initial_version=state.version,
                session_scope="session",
                runtime_preflight_cache=owner._preflight.new_cache(),
                last_runtime_preflight=prior,
                recorder=BufferingRecorder(),
                progress=None,
                repair_turns_used=0,
                advisor_checkpoint_passes_used=0,
                advisor_review_state=_AdvisorReviewState(),
                persisted_assistant_message_id=None,
                persisted_assistant_content=None,
                persisted_tool_call_turn=False,
                plugin_snapshot=None,
                deadline=None,
                composition_turns_used=0,
                discovery_turns_used=0,
                failed_turn=None,
                completion_gates=None,
                provider_owner=None,
                analysis=analysis,
            )
            assert outcome.action == "return"
            assert outcome.result is not None
            assert outcome.result.runtime_preflight is not None
            assert outcome.result.runtime_preflight.errors[0].error_code == "source_blob_identity_invalid"
            assert orphan.await_args.kwargs["analysis"] is analysis
            assert gate.await_args.kwargs["analysis"] is analysis
    assert len(validation_threads) == (0 if has_runtime_prior else 1 if shared_analysis else 2)
    assert all(thread != loop_thread for thread in validation_threads)
    # The graph summary is reusable; the terminal duties still execute each time.
    assert orphan.await_count == 2
    assert gate.await_count == 2
    await registry.finish_shutdown()


@pytest.mark.asyncio
async def test_structural_preflight_uses_exact_cache_key_and_keeps_runtime_verdict_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(tmp_path)
    state = make_small_composer_state()
    changed = replace(state, nodes=(), version=state.version)
    runtime = _runtime_result(valid=False)
    live_preflight = AsyncMock(spec=owner._preflight.cached_runtime_preflight, return_value=runtime)
    monkeypatch.setattr(owner._preflight, "cached_runtime_preflight", live_preflight)
    loop_thread = threading.get_ident()
    validations: list[tuple[CompositionState, int]] = []
    original_validate = CompositionState.validate

    def tracked_validate(current: CompositionState) -> ValidationSummary:
        validations.append((current, threading.get_ident()))
        return original_validate(current)

    monkeypatch.setattr(CompositionState, "validate", tracked_validate)
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(SessionCacheKey("principal", "session")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        for current in (state, state, changed, changed):
            result = await owner._preflight.turn_runtime_preflight(
                state=current,
                user_id=None,
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=owner._preflight.new_cache(),
                initial_version=state.version,
                session_scope="session",
                recorder=BufferingRecorder(),
                analysis=analysis,
            )
            assert result is (runtime if current is changed else None)
    assert [current for current, _ in validations] == [state, changed]
    assert all(thread != loop_thread for _, thread in validations)
    assert live_preflight.await_count == 2
    await registry.finish_shutdown()


@pytest.mark.asyncio
async def test_repair_and_finalization_share_the_operation_raw_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path)
    state = make_small_composer_state()
    original_validate = CompositionState.validate
    validation_calls = 0

    def tracked_validate(current: CompositionState) -> ValidationSummary:
        nonlocal validation_calls
        validation_calls += 1
        return original_validate(current)

    monkeypatch.setattr(CompositionState, "validate", tracked_validate)
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(SessionCacheKey("principal", "session")) as lease:
        analysis = ComposerAnalysis(lease.cache)
        assert not await owner._completion._attempt_preflight_repair(
            state=state,
            llm_messages=[],
            user_id=None,
            session_id=None,
            last_runtime_preflight=None,
            runtime_preflight_cache=owner._preflight.new_cache(),
            initial_version=state.version,
            session_scope="session",
            recorder=BufferingRecorder(),
            repair_turns_used=0,
            analysis=analysis,
        )
        result = await owner._completion._finalize_no_tool_response(
            content="Here is the current draft.",
            state=state,
            initial_version=state.version,
            user_id=None,
            session_id=None,
            last_runtime_preflight=None,
            runtime_preflight_cache=owner._preflight.new_cache(),
            session_scope="session",
            analysis=analysis,
        )
        assert result.message == "Here is the current draft."
        assert result.runtime_preflight is None
    assert validation_calls == 1
    await registry.finish_shutdown()


@pytest.mark.asyncio
async def test_terminal_site_enumeration_runs_in_worker_with_the_same_analysis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path)
    state = make_small_composer_state()
    original_validate = CompositionState.validate
    validation_calls = 0
    loop_thread = threading.get_ident()
    site_threads: list[int] = []

    def tracked_validate(current: CompositionState) -> ValidationSummary:
        nonlocal validation_calls
        validation_calls += 1
        return original_validate(current)

    monkeypatch.setattr(CompositionState, "validate", tracked_validate)
    missing_sites = AsyncMock(spec=owner._interpretation_surfacing._missing_pending_interpretation_review_sites, return_value=())
    monkeypatch.setattr(owner._interpretation_surfacing, "_missing_pending_interpretation_review_sites", missing_sites)
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(SessionCacheKey("principal", "session")) as lease:
        analysis = ComposerAnalysis(lease.cache)

        def enumerate_sites(current: CompositionState, *, raw_validator: Callable[[CompositionState], ValidationSummary]) -> tuple[()]:
            site_threads.append(threading.get_ident())
            assert raw_validator == analysis.validate_raw
            raw_validator(current)
            return ()

        monkeypatch.setattr(composition_completion, "interpretation_sites", enumerate_sites)
        owned_worker = AsyncMock(spec=analysis.run_in_worker, wraps=analysis.run_in_worker)
        monkeypatch.setattr(analysis, "run_in_worker", owned_worker)
        result = await owner._completion._surface_and_finalize_no_tools(
            assistant_message=_AdmittedAssistantMessage(content="Here is the draft."),
            state=state,
            session_id=None,
            current_state_id=None,
            progress=None,
            recorder=BufferingRecorder(),
            initial_version=state.version,
            user_id=None,
            last_runtime_preflight=None,
            runtime_preflight_cache=owner._preflight.new_cache(),
            session_scope="session",
            message="Inspect the draft.",
            mutation_success_seen=False,
            repair_turns_used=0,
            analysis=analysis,
        )
        assert result.message == "Here is the draft."
        assert missing_sites.await_args.kwargs["analysis"] is analysis
        owned_worker.assert_any_await(enumerate_sites, state, raw_validator=analysis.validate_raw)
    assert len(site_threads) == 1
    assert site_threads[0] != loop_thread
    assert validation_calls == 1
    await registry.finish_shutdown()
