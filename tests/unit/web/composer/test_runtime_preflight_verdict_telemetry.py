# tests/unit/web/composer/test_runtime_preflight_verdict_telemetry.py
"""A red preflight verdict must not be recorded as a success (elspeth-ca0bd5d4ef).

``_cached_runtime_preflight`` emitted ``outcome="failure"`` only when the cache
entry was a ``RuntimePreflightFailure`` — an EXCEPTION. A normally-returned
``ValidationResult(is_valid=False)``, a genuine red verdict, fell through to
``outcome="success"``, so "the preflight ran and said no" was indistinguishable
from "the preflight ran and said yes" on every observability surface.

The split records the two orthogonal facts separately: ``outcome`` is how the
call ended (returned vs threw) and ``verdict`` is what a returned preflight
said. The second half adds ``composer.preflight_invalid_finalize.total``, which
answers a question nothing could answer before — when the validator's objection
reaches the operator, had the shared repair budget already been spent? If it
usually had, the model never received the actionable objection at all.

The shared ValidationResult shapes and the two-armed ``validate_pipeline`` fake
are imported from the sibling module rather than duplicated; both files are
halves of the same finalize-tail contract and must not drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.web.composer import service as service_module
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import ComposerServiceImpl, _preflight_verdict
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import ValidationResult

from ._helpers import _empty_state, _make_settings, _mock_catalog
from .test_no_tool_finalize_universal_qualification import (
    _handoff_result,
    _non_empty_state,
    _RecordingValidatePipeline,
    _structural_failure_result,
    _valid_result,
)


class _FakeCounter:
    def __init__(self) -> None:
        self.emitted: list[tuple[int, dict[str, Any]]] = []

    def add(self, value: int, attributes: dict[str, Any]) -> None:
        self.emitted.append((value, dict(attributes)))


class TestPreflightVerdictVocabulary:
    def test_valid_result_is_valid(self) -> None:
        assert _preflight_verdict(_valid_result()) == "valid"

    def test_red_result_is_invalid(self) -> None:
        assert _preflight_verdict(_structural_failure_result()) == "invalid"

    def test_pending_handoff_is_its_own_verdict(self) -> None:
        """``is_valid=False`` but authoring-valid — folding it into "invalid"
        would re-commit the collapsed-bucket defect one level down."""
        assert _preflight_verdict(_handoff_result()) == "pending_review"


class TestCachedRuntimePreflightTelemetry:
    async def _emit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        strict: ValidationResult,
        interpretation_tolerant: bool = False,
    ) -> list[tuple[int, dict[str, Any]]]:
        counter = _FakeCounter()
        monkeypatch.setattr(service_module, "_RUNTIME_PREFLIGHT_COUNTER", counter)
        monkeypatch.setattr(
            service_module,
            "validate_pipeline",
            _RecordingValidatePipeline(strict=strict, tolerant=_valid_result()),
        )
        service = ComposerServiceImpl.for_trained_operator(
            catalog=_mock_catalog(),
            settings=_make_settings(data_dir=tmp_path),
        )
        await service._cached_runtime_preflight(
            _empty_state(),
            user_id="alice",
            session_id=None,
            cache=service._new_runtime_preflight_cache(),
            initial_version=1,
            session_scope="session:test",
            interpretation_tolerant=interpretation_tolerant,
        )
        return counter.emitted

    @pytest.mark.anyio
    async def test_green_verdict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        emitted = await self._emit(tmp_path, monkeypatch, strict=_valid_result())
        assert emitted == [(1, {"outcome": "returned", "verdict": "valid", "interpretation_tolerant": False})]

    @pytest.mark.anyio
    async def test_red_verdict_is_not_recorded_as_a_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The defect: a normally-returned ``is_valid=False`` used to emit
        ``outcome=success``, so "the preflight ran and said no" had no
        observability surface anywhere."""
        emitted = await self._emit(tmp_path, monkeypatch, strict=_structural_failure_result())
        assert emitted == [(1, {"outcome": "returned", "verdict": "invalid", "interpretation_tolerant": False})]
        assert all(attrs.get("outcome") != "success" for _value, attrs in emitted)

    @pytest.mark.anyio
    async def test_pending_review_verdict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        emitted = await self._emit(tmp_path, monkeypatch, strict=_handoff_result())
        assert emitted == [(1, {"outcome": "returned", "verdict": "pending_review", "interpretation_tolerant": False})]

    @pytest.mark.anyio
    async def test_tolerant_revalidation_is_marked_on_the_verdict_series(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The masked re-validation runs with ``interpretation_tolerant=True`` and
        its verdicts must not land on the strict series: the cache key carries
        the flag, so an emit without it double-counts every pending-review turn
        as a strict green (handover E-7)."""
        emitted = await self._emit(tmp_path, monkeypatch, strict=_valid_result(), interpretation_tolerant=True)
        assert emitted == [(1, {"outcome": "returned", "verdict": "valid", "interpretation_tolerant": True})]

    @pytest.mark.anyio
    async def test_raising_preflight_still_reports_the_transport_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exception arm keeps ``outcome=failure`` and carries no verdict —
        a call that threw never produced one. The AWS dashboard queries this
        exact dimension (deploy/aws-ecs/.../iam_observability.tf)."""
        counter = _FakeCounter()
        monkeypatch.setattr(service_module, "_RUNTIME_PREFLIGHT_COUNTER", counter)

        def _boom(*_args: Any, **_kwargs: Any) -> ValidationResult:
            raise ValueError("validator exploded")

        monkeypatch.setattr(service_module, "validate_pipeline", _boom)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=_mock_catalog(),
            settings=_make_settings(data_dir=tmp_path),
        )
        with pytest.raises(service_module.ComposerRuntimePreflightError):
            await service._cached_runtime_preflight(
                _empty_state(),
                user_id="alice",
                session_id=None,
                cache=service._new_runtime_preflight_cache(),
                initial_version=1,
                session_scope="session:test",
            )

        assert len(counter.emitted) == 1
        _value, attributes = counter.emitted[0]
        assert attributes["outcome"] == "failure"
        assert "verdict" not in attributes


class TestPreflightInvalidFinalizeTelemetry:
    """``composer.preflight_invalid_finalize.total`` — was the repair budget
    already spent when the validator's objection reached the operator?"""

    async def _finalize(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        strict: ValidationResult,
        last_runtime_preflight: ValidationResult | None,
        repair_turns_used: int,
        state: CompositionState | None = None,
        message: str = "build it",
    ) -> tuple[list[tuple[int, dict[str, Any]]], ComposerResult]:
        counter = _FakeCounter()
        monkeypatch.setattr(service_module, "_PREFLIGHT_INVALID_FINALIZE_COUNTER", counter)
        monkeypatch.setattr(
            service_module,
            "validate_pipeline",
            _RecordingValidatePipeline(strict=strict, tolerant=_valid_result()),
        )
        service = ComposerServiceImpl.for_trained_operator(
            catalog=_mock_catalog(),
            settings=_make_settings(data_dir=tmp_path),
        )

        class _AssistantMessage:
            content = "Here is what I attempted."

        # session_id=None makes the PT auto-surfacer and the orphan gate no-ops,
        # isolating the finalize tail. ``last_runtime_preflight`` is passed
        # SEPARATELY from ``strict`` rather than reusing it: the empty-state
        # cases below need "no preflight ever ran this turn" (None) while the
        # patched validator still has to return something, and collapsing the
        # two would make that shape unreachable.
        result = await service._surface_and_finalize_no_tools(
            assistant_message=_AssistantMessage(),
            state=_non_empty_state() if state is None else state,
            session_id=None,
            current_state_id=None,
            progress=None,
            recorder=BufferingRecorder(),
            initial_version=1,
            user_id="alice",
            last_runtime_preflight=last_runtime_preflight,
            runtime_preflight_cache=service._new_runtime_preflight_cache(),
            session_scope="session:test",
            message=message,
            mutation_success_seen=False,
            repair_turns_used=repair_turns_used,
        )
        return counter.emitted, result

    @pytest.mark.anyio
    async def test_red_verdict_with_budget_left(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        red = _structural_failure_result()
        emitted, _result = await self._finalize(tmp_path, monkeypatch, strict=red, last_runtime_preflight=red, repair_turns_used=0)
        assert emitted == [(1, {"budget_exhausted": False, "repair_turns_used": 0})]

    @pytest.mark.anyio
    async def test_red_verdict_with_budget_exhausted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The question this metric exists to answer: the model never received
        the actionable objection, because the shared repair budget was already
        spent by the time the preflight went red."""
        red = _structural_failure_result()
        emitted, _result = await self._finalize(
            tmp_path,
            monkeypatch,
            strict=red,
            last_runtime_preflight=red,
            repair_turns_used=service_module._MAX_REPAIR_TURNS,
        )
        assert emitted == [(1, {"budget_exhausted": True, "repair_turns_used": service_module._MAX_REPAIR_TURNS})]

    @pytest.mark.anyio
    async def test_green_verdict_emits_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        green = _valid_result()
        emitted, _result = await self._finalize(tmp_path, monkeypatch, strict=green, last_runtime_preflight=green, repair_turns_used=0)
        assert emitted == []

    @pytest.mark.anyio
    async def test_pending_handoff_is_not_a_red_finalize(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A user-action boundary is not a validator objection. Counting it here
        would answer a different question than the one asked."""
        handoff = _handoff_result()
        emitted, _result = await self._finalize(tmp_path, monkeypatch, strict=handoff, last_runtime_preflight=handoff, repair_turns_used=0)
        assert emitted == []

    @pytest.mark.anyio
    async def test_synthesized_empty_state_red_does_not_count_as_an_invalid_finalize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counter's numerator must not be diluted by a red nothing could
        have repaired (elspeth-ebdea1112b).

        No preflight ran this turn at all — ``last_runtime_preflight`` is None
        and the state never mutated. The only red in play is the one
        ``no_tool_finalize`` SYNTHESIZES for the no-mutation empty-state
        augmentation branch, and it arrives at ``repair_turns_used=0`` BY
        CONSTRUCTION: ``_attempt_preflight_repair`` returns False
        on a structurally empty state, so the repair loop can never have spent
        budget on this turn. Counting it answers "how often does the composer
        finalize on an empty pipeline", not "was the budget already spent when
        the objection reached the operator".
        """
        emitted, result = await self._finalize(
            tmp_path,
            monkeypatch,
            strict=_valid_result(),
            last_runtime_preflight=None,
            state=_empty_state(),
            message="build me a pipeline",  # carries a build action
            repair_turns_used=0,
        )
        # Anti-vacuity: a red really was published to the operator on this
        # turn. Without this the test would also pass if the augmentation
        # branch stopped synthesizing a verdict altogether.
        assert result.runtime_preflight is not None
        assert not result.runtime_preflight.is_valid
        assert emitted == []

    @pytest.mark.anyio
    async def test_real_red_on_an_empty_state_does_not_count_either(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The deliberate half of the exclusion (elspeth-ebdea1112b).

        Here the red is a GENUINE validator objection carried in on
        ``last_runtime_preflight``, and finalize takes the
        ``preflight_invalid_empty_state_augmentation`` branch. It is excluded
        anyway, and not by accident: the repair loop's empty-state guard is
        unconditional, so this finalize could not have spent budget either. The
        exclusion is keyed on the same predicate as that guard precisely so the
        two cannot drift.

        The message deliberately carries no build action, which is what routes
        an empty state past the no-mutation augmentation branch to this one.
        """
        red = _structural_failure_result()
        emitted, result = await self._finalize(
            tmp_path,
            monkeypatch,
            strict=red,
            last_runtime_preflight=red,
            state=_empty_state(),
            message="why is it still red?",
            repair_turns_used=0,
        )
        # Identity, not just is_valid: proves the published verdict is the real
        # objection and not the synthesized one from the branch above.
        assert result.runtime_preflight is red
        assert emitted == []
