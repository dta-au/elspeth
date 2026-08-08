# tests/unit/web/composer/test_runtime_preflight_pending_review_verification.py
"""The pending-review handoff must be a VERIFIED claim before it is announced (elspeth-5a372d3267).

``review_interpretations`` fails the strict validation ledger at canonical
index 10, stamping every later stage — including ``graph_structure`` at 21 —
``SKIPPED_AFTER_FAILURE``, while asserting ``completion_ready=True`` in its
readiness. Announcing "ready for the required review" from that truncated
result is unverified (battery-2026-08-04 g08: compose published ready, the
operator resolved the reviews, and only then did /validate fail
graph_structure on a queue edge).

The fix runs a second, authoring-masked preflight
(``allow_pending_interpretation_placeholders=True``) behind every handoff
announcement and qualifies the published message when that pass is red. The
STAGED-review handoff (the model's tool batch terminated on a successful
``request_interpretation_review``) still returns to the user without extra
model turns (see test_compose_loop_interpretation_review_dispatch's
no-extra-turns pin). The NO-TOOL completion claim is different
(elspeth-ac85b0ab0e, battery round 7 g03): when the model claims done over a
handoff-shaped strict preflight, ``_attempt_preflight_repair`` now verifies
the handoff via the same masked re-validation, and masked failures are
repaired like any other contract violation instead of riding the handoff
carve-out to a terminal that names only the review cards.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.web.composer import service as service_module
from elspeth.web.composer.no_tool_policy import (
    _ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE,
)
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import (
    ComposerServiceImpl,
    _append_interpretation_review_handoff_message,
    _replace_advisor_repair_public_result,
)
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution.schemas import (
    CHECK_ADVISOR_SIGNOFF,
    ValidationCheck,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_PENDING_CODE
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

from ._helpers import _empty_state, _make_settings, _mock_catalog


def _handoff_result() -> ValidationResult:
    """The truncated-ledger shape review_interpretations produces at stage 10."""
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
                    component_id="llm_classify",
                    component_type="transform",
                    detail="vague_term review pending for transform 'llm_classify': cool",
                )
            ],
        ),
    )


def _structural_failure_result() -> ValidationResult:
    """A graph_structure-class failure the masked re-validation surfaces."""
    from elspeth.web.execution.schemas import ValidationError

    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="mapper",
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
                    component_id="mapper",
                    component_type="transform",
                    detail="consumer requires ['llm_response'], producer guarantees (none - dynamic schema)",
                )
            ],
        ),
    )


def _signoff_failed_handoff_result() -> ValidationResult:
    """The handoff shape after the END gate appended its failed advisor check.

    ``_advisor_signoff_pending_handoff_validation`` (elspeth-66717f0c99)
    preserves the handoff whole and appends only the failed
    ``advisor_signoff`` check — readiness untouched.
    """
    base = _handoff_result()
    return base.model_copy(
        update={
            "checks": [
                *base.checks,
                ValidationCheck(
                    name=CHECK_ADVISOR_SIGNOFF,
                    passed=False,
                    detail="Completion advisory review could not be obtained.",
                    affected_nodes=(),
                    outcome_code=None,
                ),
            ]
        }
    )


def _nonempty_state(version: int = 2) -> CompositionState:
    """A WIRED but Stage-1-invalid state (dangling source route).

    ``_attempt_preflight_repair`` short-circuits on structurally empty states.
    At the default ``version=2`` (> ``initial_version=1``) the gate computes
    this turn's strict preflight; at ``version=1`` the state is UNMUTATED this
    turn and only the cross-turn Stage-1 arm (elspeth-ac85b0ab0e) can trigger
    a preflight — which requires a wired pipeline (sources AND outputs
    present) whose own ``validate()`` is invalid. Here the source routes to a
    connection nothing consumes, so Stage 1 flags the dangling route while
    both structural populations exist.
    """
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "input.csv"},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "out.csv"},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=version,
    )


def _valid_result() -> ValidationResult:
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


class _RecordingValidatePipeline:
    """Fake validate_pipeline returning strict/tolerant results per call mode."""

    def __init__(self, *, strict: ValidationResult, tolerant: ValidationResult) -> None:
        self._strict = strict
        self._tolerant = tolerant
        self.calls: list[bool] = []

    def __call__(self, *args: Any, **kwargs: Any) -> ValidationResult:
        tolerant_mode = bool(kwargs.get("allow_pending_interpretation_placeholders", False))
        self.calls.append(tolerant_mode)
        return self._tolerant if tolerant_mode else self._strict


@pytest.fixture
def service(tmp_path) -> ComposerServiceImpl:
    return ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=_make_settings(data_dir=tmp_path),
    )


def _handoff_composer_result() -> ComposerResult:
    return ComposerResult(
        message="Done — interpretation review is pending.",
        state=_empty_state(),
        runtime_preflight=_handoff_result(),
        raw_assistant_content=None,
    )


class TestAppendHandoffMessage:
    def test_bare_suffix_without_findings(self) -> None:
        result = _append_interpretation_review_handoff_message(_handoff_composer_result(), "Done — interpretation review is pending.")
        assert "Review the pending assumptions to continue." in result.message
        assert "must be fixed before this pipeline can run" not in result.message

    def test_qualified_suffix_with_findings(self) -> None:
        result = _append_interpretation_review_handoff_message(
            _handoff_composer_result(),
            "Done — interpretation review is pending.",
            outstanding_findings=_structural_failure_result(),
        )
        assert "must be fixed before this pipeline can run" in result.message
        assert "(none - dynamic schema)" in result.message


class TestAdvisorRepairPublicResult:
    def test_bare_review_message_without_findings(self) -> None:
        published = _replace_advisor_repair_public_result(_handoff_composer_result())
        assert published.message == service_module._ADVISOR_REPAIR_REVIEW_PUBLIC_MESSAGE

    def test_qualified_message_with_findings(self) -> None:
        published = _replace_advisor_repair_public_result(
            _handoff_composer_result(),
            outstanding_findings=_structural_failure_result(),
        )
        assert "ready for the required review" not in published.message
        assert "must be fixed before this pipeline can run" in published.message
        assert "(none - dynamic schema)" in published.message

    def _signoff_failed_composer_result(self) -> ComposerResult:
        return ComposerResult(
            message="internal repair-cohort prose",
            state=_empty_state(),
            runtime_preflight=_signoff_failed_handoff_result(),
            raw_assistant_content=None,
        )

    def test_signoff_failed_handoff_without_findings_keeps_bare_notice(self) -> None:
        published = _replace_advisor_repair_public_result(self._signoff_failed_composer_result())
        assert _ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE in published.message
        assert "(none - dynamic schema)" not in published.message

    def test_signoff_failed_handoff_names_outstanding_findings(self) -> None:
        """elspeth-ac85b0ab0e: the signoff-failed sub-branch must not drop findings.

        Battery round 7 g03 terminated on the bare pending-handoff notice while
        the masked re-validation would have named an edge-contract violation —
        this branch short-circuited BEFORE the ``outstanding_findings`` branch,
        so the operator was told only to resolve review cards that could not
        make the pipeline runnable.
        """
        published = _replace_advisor_repair_public_result(
            self._signoff_failed_composer_result(),
            outstanding_findings=_structural_failure_result(),
        )
        assert _ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE in published.message
        assert "(none - dynamic schema)" in published.message


class TestAttemptPreflightRepairHandoffVerification:
    """The repair gate must verify a handoff-shaped preflight before standing aside.

    elspeth-ac85b0ab0e (battery round 7, g03): the strict ledger halts at
    ``review_interpretations`` before the graph/schema stages, so a
    handoff-shaped result cannot support the claim "the review is all that
    remains". The gate previously honoured that claim unverified and returned
    False, letting the loop terminate over a composition whose persisted
    record it had been correctly told is invalid.
    """

    async def _attempt(
        self,
        service: ComposerServiceImpl,
        fake: _RecordingValidatePipeline,
        monkeypatch,
        *,
        repair_turns_used: int = 0,
    ) -> tuple[bool, list[dict[str, Any]]]:
        monkeypatch.setattr(service_module, "validate_pipeline", fake)
        llm_messages: list[dict[str, Any]] = []
        fired = await service._attempt_preflight_repair(
            state=_nonempty_state(),
            llm_messages=llm_messages,
            user_id="user-1",
            session_id=None,
            last_runtime_preflight=None,
            runtime_preflight_cache=service._new_runtime_preflight_cache(),
            initial_version=1,
            session_scope="session:test",
            recorder=SimpleNamespace(llm_calls=()),
            repair_turns_used=repair_turns_used,
            plugin_snapshot=MagicMock(spec=PluginAvailabilitySnapshot),
        )
        return fired, llm_messages

    @pytest.mark.anyio
    async def test_unverified_handoff_injects_repair_from_masked_findings(self, service, monkeypatch) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_structural_failure_result())
        fired, llm_messages = await self._attempt(service, fake, monkeypatch)
        assert fired is True
        assert fake.calls == [False, True]
        assert len(llm_messages) == 1
        assert llm_messages[0]["role"] == "user"
        assert "Pre-finalisation runtime preflight" in llm_messages[0]["content"]
        assert "(none - dynamic schema)" in llm_messages[0]["content"]

    @pytest.mark.anyio
    async def test_verified_pure_handoff_stands_aside(self, service, monkeypatch) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_valid_result())
        fired, llm_messages = await self._attempt(service, fake, monkeypatch)
        assert fired is False
        assert fake.calls == [False, True]
        assert llm_messages == []

    @pytest.mark.anyio
    async def test_cross_turn_invalid_state_triggers_repair_on_unmutated_turn(self, service, monkeypatch) -> None:
        """elspeth-ac85b0ab0e cross-turn arm: prior-turn damage cannot finalize bare.

        A state made invalid on a PRIOR turn arrives here unmutated
        (``version == initial_version``) with no preview this turn, so the
        old gate saw ``runtime_result is None`` and stood aside — a
        prose-only "done" then finalized bare over a persisted
        ``is_valid=False`` record. The Stage-1 probe (``state.validate()``)
        now triggers the real preflight, and the repair gate fires.
        """
        fake = _RecordingValidatePipeline(strict=_structural_failure_result(), tolerant=_valid_result())
        monkeypatch.setattr(service_module, "validate_pipeline", fake)
        llm_messages: list[dict[str, Any]] = []
        fired = await service._attempt_preflight_repair(
            state=_nonempty_state(version=1),
            llm_messages=llm_messages,
            user_id="user-1",
            session_id=None,
            last_runtime_preflight=None,
            runtime_preflight_cache=service._new_runtime_preflight_cache(),
            initial_version=1,
            session_scope="session:test",
            recorder=SimpleNamespace(llm_calls=()),
            repair_turns_used=0,
            plugin_snapshot=MagicMock(spec=PluginAvailabilitySnapshot),
        )
        assert fired is True
        assert fake.calls == [False]
        assert len(llm_messages) == 1
        assert "(none - dynamic schema)" in llm_messages[0]["content"]

    @pytest.mark.anyio
    async def test_cross_turn_probe_skips_preflight_when_stage_one_is_clean(self, service, monkeypatch) -> None:
        """The Stage-1 probe must be the ONLY cost on clean conversational turns.

        An unmutated state whose own ``validate()`` passes gets no runtime
        preflight at all — the cross-turn arm exists to catch prior-turn
        damage, not to tax every chat turn with a dry-run through the engine.
        """
        fake = _RecordingValidatePipeline(strict=_structural_failure_result(), tolerant=_valid_result())
        monkeypatch.setattr(service_module, "validate_pipeline", fake)
        clean_state = MagicMock(spec=CompositionState)
        clean_state.version = 1
        clean_state.validate.return_value = SimpleNamespace(is_valid=True)
        clean_state.sources = {"source": object()}
        clean_state.nodes = ()
        clean_state.edges = ()
        clean_state.outputs = (object(),)
        result = await service._turn_runtime_preflight(
            state=clean_state,
            user_id="user-1",
            session_id=None,
            last_runtime_preflight=None,
            runtime_preflight_cache=service._new_runtime_preflight_cache(),
            initial_version=1,
            session_scope="session:test",
            recorder=SimpleNamespace(llm_calls=()),
            plugin_snapshot=MagicMock(spec=PluginAvailabilitySnapshot),
        )
        assert result is None
        assert fake.calls == []
        clean_state.validate.assert_called_once_with()

    @pytest.mark.anyio
    async def test_exhausted_budget_short_circuits_before_any_validation(self, service, monkeypatch) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_structural_failure_result())
        fired, llm_messages = await self._attempt(
            service,
            fake,
            monkeypatch,
            repair_turns_used=service_module._MAX_REPAIR_TURNS,
        )
        assert fired is False
        assert fake.calls == []
        assert llm_messages == []


class TestPendingHandoffOutstandingFindings:
    async def _findings(self, service: ComposerServiceImpl, fake: _RecordingValidatePipeline, monkeypatch) -> ValidationResult | None:
        monkeypatch.setattr(service_module, "validate_pipeline", fake)
        return await service._pending_handoff_outstanding_findings(
            _empty_state(),
            user_id="user-1",
            session_id=None,
            cache=service._new_runtime_preflight_cache(),
            initial_version=1,
            session_scope="session:test",
            plugin_snapshot=MagicMock(spec=PluginAvailabilitySnapshot),
        )

    @pytest.mark.anyio
    async def test_red_tolerant_pass_returns_findings(self, service, monkeypatch) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_structural_failure_result())
        findings = await self._findings(service, fake, monkeypatch)
        assert fake.calls == [True]
        assert findings is not None
        assert not findings.is_valid

    @pytest.mark.anyio
    async def test_green_tolerant_pass_returns_none(self, service, monkeypatch) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_valid_result())
        findings = await self._findings(service, fake, monkeypatch)
        assert fake.calls == [True]
        assert findings is None

    @pytest.mark.anyio
    async def test_tolerant_and_strict_cache_entries_do_not_collide(self, service, monkeypatch) -> None:
        """One state, one cache: the strict entry must not satisfy the tolerant lookup."""
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_structural_failure_result())
        monkeypatch.setattr(service_module, "validate_pipeline", fake)
        state = _empty_state()
        cache = service._new_runtime_preflight_cache()
        snapshot = MagicMock(spec=PluginAvailabilitySnapshot)
        strict = await service._cached_runtime_preflight(
            state,
            user_id="user-1",
            session_id=None,
            cache=cache,
            initial_version=1,
            session_scope="session:test",
            plugin_snapshot=snapshot,
        )
        tolerant = await service._cached_runtime_preflight(
            state,
            user_id="user-1",
            session_id=None,
            cache=cache,
            initial_version=1,
            session_scope="session:test",
            plugin_snapshot=snapshot,
            interpretation_tolerant=True,
        )
        assert fake.calls == [False, True]
        assert strict is not tolerant
        assert any(blocker.code == INTERPRETATION_REVIEW_PENDING_CODE for blocker in strict.readiness.blockers)
        assert all(blocker.code != INTERPRETATION_REVIEW_PENDING_CODE for blocker in tolerant.readiness.blockers)
