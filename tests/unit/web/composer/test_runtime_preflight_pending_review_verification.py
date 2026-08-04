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
self-repair loop is deliberately NOT engaged — the staged-review handoff
contract returns to the user without extra model turns (see
test_compose_loop_interpretation_review_dispatch's no-extra-turns pin).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.web.composer import service as service_module
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import (
    ComposerServiceImpl,
    _append_interpretation_review_handoff_message,
    _replace_advisor_repair_public_result,
)
from elspeth.web.execution.schemas import (
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
