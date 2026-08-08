"""Planner-announce Stage-2 truthfulness (elspeth-2ed41f0a4a).

``_stage_pipeline_plan`` published ``"I prepared and validated the requested
pipeline"`` over a proposal whose runtime-equivalent preflight had NEVER RUN:
every ``runtime_preflight`` slot on the planner -> stage -> commit chain was
wired ``None``, so "validated" meant the Stage-1 authoring pass alone. On the
auto-commit arm that unverified claim also became canonical state without a
human ever reading it.

This is the ticket's "unswept parity class" on its highest-risk surface: a
readiness claim measured against the authoring validator rather than the
runtime preflight.

The contract these tests pin, per the Shape-14 report-don't-block posture:

* Stage 2 GREEN     -> "validated" wording, auto-commit intent preserved.
* Stage 2 FINDINGS  -> auto-commit DOWNGRADED to review, findings wording, no
  ``PipelineCommitIntent``. The proposal still stages — a human reviews it.
* Stage 2 PENDING   -> the interpretation handoff shape. Also ``is_valid=False``
  but NOT a validator objection, so it gets its own wording; still no
  auto-commit, because an unresolved review must not become canonical.
* Stage 2 NOT-RUN   -> (raised, timed out, or no candidate state) fail-closed
  not-run wording, no auto-commit. Never reads as validated.

In every arm the verdict rides the ``ComposerResult.runtime_preflight`` field
so the UI reads the findings structurally rather than from prose.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from elspeth.core.canonical import stable_hash
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PlannerSurface
from elspeth.web.composer.protocol import (
    PIPELINE_STAGED_AUTO_COMMIT_MESSAGE,
    PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE,
    PIPELINE_STAGED_REVIEW_MESSAGE,
    PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE,
    PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE,
    ComposerRuntimePreflightError,
)
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import (
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_PENDING_CODE
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from tests.unit.web.composer._helpers import _empty_state, _make_settings

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Stage-2 verdict shapes
# ---------------------------------------------------------------------------


def _green_result() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )


def _red_result() -> ValidationResult:
    """A graph_structure objection — a genuine validator red."""
    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="map_node",
                component_type="transform",
                message="consumer requires ['llm_response'], producer guarantees (none - dynamic schema)",
                suggestion=None,
                error_code="graph_structure",
            )
        ],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code="graph_structure",
                    component_id="map_node",
                    component_type="transform",
                    detail="consumer requires ['llm_response'], producer guarantees (none - dynamic schema)",
                )
            ],
        ),
    )


def _pending_review_result() -> ValidationResult:
    """The pending-interpretation handoff: is_valid=False but completion_ready.

    Not a validator objection — a user-action boundary. It must still block
    auto-commit (nothing unreviewed becomes canonical), which is why the
    predicate cannot be a bare ``is_valid`` test.
    """
    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=False,
            completion_ready=True,
            blockers=[
                ValidationReadinessBlocker(
                    code=INTERPRETATION_REVIEW_PENDING_CODE,
                    component_id="map_node",
                    component_type="transform",
                    detail="vague_term review pending for transform 'map_node': cool",
                )
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Fakes — modelled on the real contracts (no getattr probing anywhere)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakePreferences:
    trust_mode: str


@dataclass(frozen=True, slots=True)
class _FakeProposalRow:
    id: UUID


class _FakeSessionsService:
    """Models only the two methods ``_stage_pipeline_plan`` calls."""

    def __init__(self, trust_mode: str) -> None:
        self._trust_mode = trust_mode
        self.created_proposals: list[dict[str, Any]] = []

    async def get_composer_preferences(self, session_id: UUID) -> _FakePreferences:
        del session_id
        return _FakePreferences(trust_mode=self._trust_mode)

    async def create_pipeline_composition_proposal(self, **kwargs: Any) -> _FakeProposalRow:
        self.created_proposals.append(kwargs)
        return _FakeProposalRow(id=uuid4())


def _plan(candidate_state: CompositionState | None) -> PipelinePlanResult:
    proposal = PipelineProposal.create(
        pipeline={"sources": {}, "nodes": [], "edges": [], "outputs": []},
        base=AbsentBase(),
        reviewed_facts={},
        surface=PlannerSurface.FREEFORM,
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )
    return PipelinePlanResult(
        proposal=proposal,
        tool_call_id="call_pipeline",
        custody_result="not_required",
        model_identifier="planner-model",
        model_version="planner-model-v1",
        provider="test",
        candidate_state=candidate_state,
    )


def _service() -> ComposerServiceImpl:
    return ComposerServiceImpl(
        catalog=MagicMock(),
        settings=_make_settings(),
        plugin_snapshot_factory=MagicMock(),
        operator_profile_registry=MagicMock(spec=OperatorProfileRegistry),
    )


async def _stage(
    *,
    trust_mode: str,
    preflight: ValidationResult | BaseException | None,
    candidate_state: CompositionState | None,
) -> tuple[Any, _FakeSessionsService]:
    """Run ``_stage_pipeline_plan`` with a scripted Stage-2 outcome."""
    service = _service()
    sessions = _FakeSessionsService(trust_mode)
    state = _empty_state()

    if isinstance(preflight, BaseException):
        preflight_mock = AsyncMock(side_effect=preflight)
    else:
        preflight_mock = AsyncMock(return_value=preflight)

    with (
        patch.object(service, "_sessions_service", sessions),
        patch.object(service, "_persist_pipeline_planner_audit", new_callable=AsyncMock),
        patch.object(service, "_cached_runtime_preflight", preflight_mock),
    ):
        result = await service._stage_pipeline_plan(
            plan=_plan(candidate_state),
            state=state,
            session_id=uuid4(),
            current_state_id=None,
            user_message_id=uuid4(),
            user_id="user-1",
            preferences=_FakePreferences(trust_mode=trust_mode),
            recorder=MagicMock(llm_calls=(), invocations=()),
            plugin_snapshot=None,
        )
    return result, sessions


# ---------------------------------------------------------------------------
# GREEN — the only arm allowed to say "validated"
# ---------------------------------------------------------------------------


async def test_green_preflight_keeps_auto_commit_and_validated_wording() -> None:
    result, _ = await _stage(
        trust_mode="auto_commit",
        preflight=_green_result(),
        candidate_state=_empty_state(),
    )

    assert result.message == PIPELINE_STAGED_AUTO_COMMIT_MESSAGE
    assert result.pipeline_commit_intent is not None
    # The verdict rides the envelope, not just the prose.
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.is_valid is True


async def test_green_preflight_under_explicit_approve_uses_validated_review_wording() -> None:
    result, _ = await _stage(
        trust_mode="explicit_approve",
        preflight=_green_result(),
        candidate_state=_empty_state(),
    )

    assert result.message == PIPELINE_STAGED_REVIEW_MESSAGE
    assert result.pipeline_commit_intent is None
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.is_valid is True


# ---------------------------------------------------------------------------
# FINDINGS — report, do not block; never auto-commit
# ---------------------------------------------------------------------------


async def test_red_preflight_downgrades_auto_commit_to_review() -> None:
    result, sessions = await _stage(
        trust_mode="auto_commit",
        preflight=_red_result(),
        candidate_state=_empty_state(),
    )

    # Downgraded, NOT hard-failed: the proposal still staged for a human.
    assert len(sessions.created_proposals) == 1
    assert result.pipeline_commit_intent is None
    assert result.message == PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE
    assert result.message != PIPELINE_STAGED_AUTO_COMMIT_MESSAGE
    assert "validated" not in result.message
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.is_valid is False


async def test_red_preflight_on_review_arm_is_truthful() -> None:
    result, _ = await _stage(
        trust_mode="explicit_approve",
        preflight=_red_result(),
        candidate_state=_empty_state(),
    )

    assert result.message == PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE
    assert result.message != PIPELINE_STAGED_REVIEW_MESSAGE
    assert result.pipeline_commit_intent is None


async def test_pending_interpretation_review_blocks_auto_commit() -> None:
    """A pending review is not a validator objection, but is not runnable either.

    A bare ``completion_ready`` predicate would auto-commit it unreviewed, so
    it must downgrade like any other non-green verdict.
    """
    result, _ = await _stage(
        trust_mode="auto_commit",
        preflight=_pending_review_result(),
        candidate_state=_empty_state(),
    )

    assert result.pipeline_commit_intent is None
    assert "validated" not in result.message


async def test_pending_interpretation_review_is_not_reported_as_findings() -> None:
    """The pending handoff gets its OWN wording, not the validator-objection one.

    Both shapes are ``is_valid=False``, so a predicate that split on validity
    alone would tell the operator to fix "issues" when the only outstanding
    work is resolving a review card. That is the ticket's over-claim in mirror
    image: a truthful-sounding message pointing at a defect that is not there.
    """
    result, _ = await _stage(
        trust_mode="auto_commit",
        preflight=_pending_review_result(),
        candidate_state=_empty_state(),
    )

    assert result.message == PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE
    assert result.message != PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE
    assert "must be fixed" not in result.message


# ---------------------------------------------------------------------------
# NOT-RUN — fail closed; must never read as validated
# ---------------------------------------------------------------------------


async def test_preflight_failure_is_not_run_and_blocks_auto_commit() -> None:
    """A raising preflight reports; it does not 500 an otherwise stageable plan."""
    result, sessions = await _stage(
        trust_mode="auto_commit",
        preflight=ComposerRuntimePreflightError(
            original_exc=RuntimeError("preflight exploded"),
            partial_state=None,
        ),
        candidate_state=_empty_state(),
    )

    assert len(sessions.created_proposals) == 1
    assert result.pipeline_commit_intent is None
    assert result.message == PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE
    assert "validated" not in result.message
    # Not-run is distinguishable from a red verdict: no ValidationResult exists.
    assert result.runtime_preflight is None


async def test_preflight_timeout_is_not_run() -> None:
    """A timeout reaches the caller as ComposerRuntimePreflightError, not TimeoutError.

    ``RuntimePreflightCoordinator._capture`` converts every ``Exception`` into
    a ``RuntimePreflightFailure``, which ``_cached_runtime_preflight`` re-raises
    in that single envelope. Scripting a bare ``TimeoutError`` here would test
    a path production cannot produce and would license a dead ``except`` arm.
    """
    result, _ = await _stage(
        trust_mode="auto_commit",
        preflight=ComposerRuntimePreflightError(
            original_exc=TimeoutError("deadline expired"),
            partial_state=None,
        ),
        candidate_state=_empty_state(),
    )

    assert result.pipeline_commit_intent is None
    assert result.message == PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE


async def test_cancellation_is_not_swallowed_into_a_staged_proposal() -> None:
    """CancelledError is a BaseException — it must abort, not stage.

    Broadening the ``except`` to ``Exception`` (or catching ``BaseException``
    defensively) would turn a cancelled request into a staged proposal carrying
    a verdict nobody waited for.
    """
    with pytest.raises(asyncio.CancelledError):
        await _stage(
            trust_mode="auto_commit",
            preflight=asyncio.CancelledError(),
            candidate_state=_empty_state(),
        )


async def test_absent_candidate_state_is_not_run_not_validated() -> None:
    """No candidate state means Stage 2 had nothing to measure — fail closed."""
    result, _ = await _stage(
        trust_mode="auto_commit",
        preflight=_green_result(),
        candidate_state=None,
    )

    assert result.pipeline_commit_intent is None
    assert result.message == PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE
    assert "validated" not in result.message


# ---------------------------------------------------------------------------
# The claim itself
# ---------------------------------------------------------------------------


def test_only_the_green_constants_claim_validation() -> None:
    """The word "validated" is reserved for a green Stage-2 verdict."""
    assert "validated" in PIPELINE_STAGED_AUTO_COMMIT_MESSAGE
    assert "validated" in PIPELINE_STAGED_REVIEW_MESSAGE
    assert "validated" not in PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE
    assert "validated" not in PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE
    assert "validated" not in PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE
