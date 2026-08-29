"""Advisor terminal publication: branch attribution + withheld-prose disclosure.

Two sibling defects from live session 39578c6f (2026-08-28):

* elspeth-fa18d54eef — a live turn published the pending-handoff "did not
  clear" notice under a journal trail (advisor pass CLEAN, no withheld
  disclosure row) that this tree cannot produce. Which terminal branch
  published which message was UNOBSERVABLE after the fact: the fix is a
  best-effort structured event, ``composer.advisor_terminal_publication``,
  emitted at every advisor-cohort publication site naming the branch, so any
  recurrence is attributable from one journal read.

* elspeth-ff4f0068a4 — the repair cohort replaces the model's prose with
  fixed backend copy, so a turn in which the composer explained that a user
  instruction could not be applied (one served LLM profile; the user asked
  for two) published nothing telling the user that. Every terminal message
  that replaces withheld prose now carries a fixed operator-authored
  disclosure: verify the pipeline before assuming every requested change was
  applied.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.composer import no_tool_policy
from elspeth.web.composer import service as service_module
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import _replace_advisor_repair_public_result
from elspeth.web.composer.state import CompositionState, PipelineMetadata

from .test_runtime_preflight_pending_review_verification import (
    _handoff_composer_result,
    _signoff_failed_handoff_result,
    _structural_failure_result,
    _valid_result,
)

_DISCLOSURE = no_tool_policy.ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE


def _empty_state() -> CompositionState:
    return CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=7, sources={})


class _RecordingPublicationTelemetry:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def publication_spy(monkeypatch: pytest.MonkeyPatch) -> _RecordingPublicationTelemetry:
    spy = _RecordingPublicationTelemetry()
    monkeypatch.setattr(service_module, "record_advisor_terminal_publication", spy)
    return spy


# ---------------------------------------------------------------------------
# elspeth-ff4f0068a4 — the withheld-prose disclosure rides every terminal
# message that replaces model prose in the advisor cohort.
# ---------------------------------------------------------------------------


class TestWithheldProseDisclosure:
    """The disclosure is one fixed sentence, present on every cohort terminal.

    The intermediate repair status line ("ELSPETH is applying a pipeline
    correction.") is deliberately excluded: it is transient progress copy, and
    the turn always ends in one of the terminals below, which is where the
    user decides what the turn did.
    """

    def test_disclosure_names_the_withholding_and_the_check(self) -> None:
        assert "withheld" in _DISCLOSURE
        assert "before assuming" in _DISCLOSURE

    @pytest.mark.parametrize(
        "message",
        [
            no_tool_policy._ADVISOR_SIGNOFF_PENDING_NOTICE,
            no_tool_policy._ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE,
            no_tool_policy.ADVISOR_REPAIR_SUCCESS_PUBLIC_MESSAGE,
            no_tool_policy.ADVISOR_REPAIR_REVIEW_PUBLIC_MESSAGE,
            no_tool_policy.ADVISOR_REPAIR_REVIEW_WITH_FINDINGS_PUBLIC_MESSAGE,
            no_tool_policy.ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE,
        ],
        ids=[
            "signoff_pending_notice",
            "pending_handoff_notice",
            "repair_success",
            "repair_review",
            "repair_review_with_findings",
            "repair_unverified",
        ],
    )
    def test_terminal_message_carries_disclosure(self, message: str) -> None:
        assert _DISCLOSURE in message

    def test_segment_recognizer_still_mints_trusted_chrome_for_extended_notices(self) -> None:
        """The finalize suffixes derive from the same constants, so the
        recognizer must keep minting trusted chrome after the extension —
        a hand-copied suffix in the recognizer would fail here."""
        bare = no_tool_policy.compose_advisor_pending_handoff_message("")
        segments = no_tool_policy.visible_message_segments(content=bare, raw_content="")
        assert segments == (no_tool_policy.TrustedSystemNoticeSegment(no_tool_policy._ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE),)

        pending = no_tool_policy.compose_advisor_signoff_pending_message("")
        segments = no_tool_policy.visible_message_segments(content=pending, raw_content="")
        assert segments == (no_tool_policy.TrustedSystemNoticeSegment(no_tool_policy._ADVISOR_SIGNOFF_PENDING_NOTICE),)


# ---------------------------------------------------------------------------
# elspeth-fa18d54eef — every publication branch is attributed via one
# best-effort structured event.
# ---------------------------------------------------------------------------


class TestRepairPublicationBranchAttribution:
    def test_unverified_branch(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        _replace_advisor_repair_public_result(
            ComposerResult(message="prose", state=_empty_state(), runtime_preflight=None),
            session_id="sess-1",
        )
        assert [c["branch"] for c in publication_spy.calls] == ["repair_unverified"]
        assert publication_spy.calls[0]["session_id"] == "sess-1"
        assert publication_spy.calls[0]["preflight_shape"] == "absent"

    def test_success_branch(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        _replace_advisor_repair_public_result(
            ComposerResult(message="prose", state=_empty_state(), runtime_preflight=_valid_result()),
            session_id="sess-2",
        )
        assert [c["branch"] for c in publication_spy.calls] == ["repair_success"]
        assert publication_spy.calls[0]["preflight_shape"] == "green"

    def test_bare_review_branch(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        _replace_advisor_repair_public_result(_handoff_composer_result(), session_id="sess-3")
        assert [c["branch"] for c in publication_spy.calls] == ["repair_review"]
        assert publication_spy.calls[0]["preflight_shape"] == "pending_handoff"

    def test_review_with_findings_branch(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        _replace_advisor_repair_public_result(
            _handoff_composer_result(),
            outstanding_findings=_structural_failure_result(),
            session_id="sess-4",
        )
        assert [c["branch"] for c in publication_spy.calls] == ["repair_review_with_findings"]

    def test_signoff_failed_handoff_branch(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_signoff_failed_handoff_result(),
                raw_assistant_content=None,
            ),
            session_id="sess-5",
        )
        assert [c["branch"] for c in publication_spy.calls] == ["repair_handoff_signoff_failed"]

    def test_preflight_failure_branch(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_structural_failure_result(),
                raw_assistant_content="prose",
            ),
            session_id="sess-6",
        )
        assert [c["branch"] for c in publication_spy.calls] == ["repair_preflight_failure"]
        assert publication_spy.calls[0]["preflight_shape"] == "red"

    def test_direct_call_without_session_still_emits(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        """The session id is attribution, not a gate: a caller that has no
        session (direct unit invocation) still records the branch."""
        _replace_advisor_repair_public_result(_handoff_composer_result())
        assert [c["branch"] for c in publication_spy.calls] == ["repair_review"]
        assert publication_spy.calls[0]["session_id"] is None


class TestBlockedTerminalBranchAttribution:
    def _blocked(self, service: Any, *, runtime_preflight: Any, session_id: str | None) -> Any:
        from elspeth.web.composer.service import AdvisorCheckpointVerdict
        from elspeth.web.composer.tool_batch import BufferingRecorder

        return service._advisor_blocked_result(
            reason="flagged_final_pass",
            verdict=AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: still wrong"),
            state=_empty_state(),
            assistant_message=None,
            recorder=BufferingRecorder(),
            repair_turns_used=0,
            persisted_assistant_message_id=None,
            persisted_tool_call_turn=False,
            runtime_preflight=runtime_preflight,
            outstanding_findings=None,
            session_id=session_id,
        )

    def test_blocked_terminal_emits_reason_and_shape(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        self._blocked(service, runtime_preflight=None, session_id="sess-7")
        assert [c["branch"] for c in publication_spy.calls] == ["terminal_block"]
        assert publication_spy.calls[0]["reason"] == "flagged_final_pass"
        assert publication_spy.calls[0]["preflight_shape"] == "absent"
        assert publication_spy.calls[0]["session_id"] == "sess-7"

    def test_blocked_handoff_terminal_reports_handoff_shape(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        self._blocked(service, runtime_preflight=_handoff_composer_result().runtime_preflight, session_id=None)
        assert [c["branch"] for c in publication_spy.calls] == ["terminal_block"]
        assert publication_spy.calls[0]["preflight_shape"] == "pending_handoff"


class TestTelemetryHelperShape:
    def test_emit_is_best_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A broken event sink must not fail the request (signed
        ``telemetry_phase8`` posture)."""
        from elspeth.web.composer import advisor_checkpoint_telemetry as telemetry

        class _ExplodingLogger:
            def info(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("exporter outage")

        monkeypatch.setattr(telemetry, "slog", _ExplodingLogger())
        telemetry.record_advisor_terminal_publication(
            session_id="sess-8",
            branch="repair_review",
            reason=None,
            preflight_shape="pending_handoff",
        )
