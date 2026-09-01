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
            no_tool_policy._ADVISOR_SIGNOFF_UNVERIFIED_NOTICE,
            no_tool_policy._ADVISOR_SIGNOFF_UNREPAIRABLE_NOTICE,
            no_tool_policy._ADVISOR_SIGNOFF_UNREPAIRABLE_UNVERIFIED_NOTICE,
            no_tool_policy._ADVISOR_SIGNOFF_UNREPAIRABLE_HANDOFF_NOTICE,
            no_tool_policy._ADVISOR_SIGNOFF_UNREPAIRABLE_RED_FOOTER,
            no_tool_policy._ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE,
            no_tool_policy.ADVISOR_REPAIR_SUCCESS_PUBLIC_MESSAGE,
            no_tool_policy.ADVISOR_REPAIR_REVIEW_PUBLIC_MESSAGE,
            no_tool_policy.ADVISOR_REPAIR_REVIEW_WITH_FINDINGS_PUBLIC_MESSAGE,
            no_tool_policy.ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE,
        ],
        ids=[
            "signoff_pending_notice",
            "signoff_unverified_notice",
            "signoff_unrepairable_notice",
            "signoff_unrepairable_unverified_notice",
            "signoff_unrepairable_handoff_notice",
            "signoff_unrepairable_red_footer",
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


def _blocked_terminal(
    service: Any,
    *,
    runtime_preflight: Any,
    session_id: str | None,
    findings_backend_authored: bool = False,
    reason: str = "flagged_final_pass",
) -> Any:
    from elspeth.web.composer.service import AdvisorCheckpointVerdict
    from elspeth.web.composer.tool_batch import BufferingRecorder

    return service._advisor_blocked_result(
        reason=reason,
        verdict=AdvisorCheckpointVerdict(
            ok=True,
            blocking=True,
            findings_text="FLAGGED: still wrong",
            findings_backend_authored=findings_backend_authored,
        ),
        state=_empty_state(),
        assistant_message=None,
        recorder=BufferingRecorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=runtime_preflight,
        outstanding_findings=None,
        session_id=session_id,
    )


class TestBlockedTerminalBranchAttribution:
    def test_blocked_terminal_emits_reason_and_shape(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        _blocked_terminal(service, runtime_preflight=None, session_id="sess-7")
        assert [c["branch"] for c in publication_spy.calls] == ["terminal_block"]
        assert publication_spy.calls[0]["reason"] == "flagged_final_pass"
        assert publication_spy.calls[0]["preflight_shape"] == "absent"
        assert publication_spy.calls[0]["session_id"] == "sess-7"

    def test_blocked_handoff_terminal_reports_handoff_shape(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        _blocked_terminal(service, runtime_preflight=_handoff_composer_result().runtime_preflight, session_id=None)
        assert [c["branch"] for c in publication_spy.calls] == ["terminal_block"]
        assert publication_spy.calls[0]["preflight_shape"] == "pending_handoff"

    def test_blocked_unrepairable_handoff_terminal_reports_reason_and_shape(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        """Fix round 1: the reason x shape cell the first matrix omitted."""
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        _blocked_terminal(
            service,
            runtime_preflight=_handoff_composer_result().runtime_preflight,
            session_id=None,
            reason="flagged_unrepairable",
            findings_backend_authored=True,
        )
        assert [c["branch"] for c in publication_spy.calls] == ["terminal_block"]
        assert publication_spy.calls[0]["reason"] == "flagged_unrepairable"
        assert publication_spy.calls[0]["preflight_shape"] == "pending_handoff"
        assert publication_spy.calls[0]["findings_backend_authored"] is True


class TestPublicationFindingsProvenance:
    """elspeth-25f7b757e7 (A2): the publication event says whether its wording
    embeds the backend-authored pre-scan finding. Only the blocked terminal can
    carry True (it is the only publication whose wording rides the verdict);
    every repair-cohort branch publishes fixed copy with no finding at all."""

    def test_blocked_terminal_reports_backend_authored_findings(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        _blocked_terminal(service, runtime_preflight=None, session_id="sess-9", findings_backend_authored=True)
        assert [c["findings_backend_authored"] for c in publication_spy.calls] == [True]

    def test_blocked_terminal_reports_model_findings_as_not_backend_authored(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        _blocked_terminal(service, runtime_preflight=None, session_id="sess-10")
        assert [c["findings_backend_authored"] for c in publication_spy.calls] == [False]

    def test_replacer_branches_report_no_backend_finding(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        _replace_advisor_repair_public_result(
            ComposerResult(message="prose", state=_empty_state(), runtime_preflight=None),
            session_id="sess-11",
        )
        assert [c["findings_backend_authored"] for c in publication_spy.calls] == [False]


# ---------------------------------------------------------------------------
# elspeth-2ae50afcd1 — an already-published END blocked terminal must not be
# re-published through the repair replacer.
# ---------------------------------------------------------------------------


class TestBlockedTerminalIsNotRepublished:
    """Observed live (session 346e0671, 2026-09-01): one blocked terminal on a
    question-only turn emitted ``terminal_block`` (preflight_shape=absent) and
    then ``repair_preflight_failure`` (preflight_shape=red) 0.2 ms apart — the
    replacer re-derived the shape from the SYNTHESIZED advisor-signoff
    validation stored in ``runtime_preflight``, double-counting the branch
    metric and reporting a red preflight for a turn whose preflight never ran.
    ``_advisor_blocked_result`` already publishes fixed backend copy and its
    own telemetry; the replacer must pass such a result through untouched."""

    def _service(self) -> Any:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        return service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())

    def test_absent_preflight_blocked_terminal_emits_exactly_one_event(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        blocked = _blocked_terminal(self._service(), runtime_preflight=None, session_id="sess-11")
        published = _replace_advisor_repair_public_result(blocked, session_id="sess-11")
        assert published == blocked
        assert [c["branch"] for c in publication_spy.calls] == ["terminal_block"]
        assert [c["preflight_shape"] for c in publication_spy.calls] == ["absent"]

    def test_handoff_preflight_blocked_terminal_emits_exactly_one_event(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        blocked = _blocked_terminal(
            self._service(),
            runtime_preflight=_handoff_composer_result().runtime_preflight,
            session_id="sess-12",
        )
        published = _replace_advisor_repair_public_result(blocked, session_id="sess-12")
        assert published == blocked
        assert [c["branch"] for c in publication_spy.calls] == ["terminal_block"]

    def test_blocked_terminal_carries_the_published_marker(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        """The pass-through derives from the producer's own positive proof,
        not from re-parsing the synthesized preflight shape (a raw-prose
        result with a genuine signoff-failed preflight must still be
        replaced — pinned by ``test_real_advisor_failure_still_publishes_the_notice``)."""
        blocked = _blocked_terminal(self._service(), runtime_preflight=None, session_id=None)
        assert blocked.advisor_terminal_published is True
        assert ComposerResult(message="prose", state=_empty_state()).advisor_terminal_published is False


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
            findings_backend_authored=False,
        )


# ---------------------------------------------------------------------------
# elspeth-fa18d54eef root cause — the strict ledger's SKIPPED advisor row is
# not an advisor verdict.
# ---------------------------------------------------------------------------


def _producer_honest_handoff_result():
    """The handoff shape as ``validate_pipeline`` ACTUALLY emits it.

    ``_skipped_checks`` (execution/validation.py) emits every check downstream
    of the halted ``review_interpretations`` stage as ``passed=False`` with
    ``outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE`` — including
    ``advisor_signoff``. The hand-built ``_handoff_result()`` fixture carries
    ``checks=[]``, which is why no scripted reproduction ever hit the live
    defect (the fixture pinned a shape no producer emits).
    """
    from elspeth.web.execution.schemas import (
        CHECK_ADVISOR_SIGNOFF,
        CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
        ValidationCheck,
    )

    base = _handoff_composer_result().runtime_preflight
    return base.model_copy(
        update={
            "checks": [
                *base.checks,
                ValidationCheck(
                    name=CHECK_ADVISOR_SIGNOFF,
                    passed=False,
                    detail="Skipped: review_interpretations failed",
                    affected_nodes=(),
                    outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE,
                ),
            ]
        }
    )


class TestSkippedLedgerRowIsNotAnAdvisorVerdict:
    """Observed live twice on 2026-08-29 (session 7afbc210, turns 2 and 4),
    and retroactively explains the original 39578c6f turn 3: a CLEAN pass-2
    fall-through published the pending-handoff "did not clear" notice because
    the discriminator read the ledger's SKIPPED advisor row as a failure."""

    def test_clean_fallthrough_over_real_handoff_publishes_review_message(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        result = ComposerResult(
            message="prose",
            state=_empty_state(),
            runtime_preflight=_producer_honest_handoff_result(),
            raw_assistant_content=None,
        )
        published = _replace_advisor_repair_public_result(result, session_id="sess-9")
        assert published.message == no_tool_policy.ADVISOR_REPAIR_REVIEW_PUBLIC_MESSAGE
        assert "did not clear" not in published.message
        assert [c["branch"] for c in publication_spy.calls] == ["repair_review"]

    def test_real_advisor_failure_still_publishes_the_notice(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        """A check the advisor path actually built (outcome_code=None) keeps
        the blocked wording — the fix must not widen into ignoring genuine
        verdicts."""
        published = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_signoff_failed_handoff_result(),
                raw_assistant_content=None,
            ),
            session_id="sess-10",
        )
        assert no_tool_policy._ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE in published.message
        assert [c["branch"] for c in publication_spy.calls] == ["repair_handoff_signoff_failed"]

    def test_predicate_distinguishes_skipped_from_failed(self) -> None:
        from elspeth.web.execution.completion_gates import advisor_signoff_check_failed

        assert advisor_signoff_check_failed(_producer_honest_handoff_result().checks) is False
        assert advisor_signoff_check_failed(_signoff_failed_handoff_result().checks) is True
        assert advisor_signoff_check_failed([]) is False


# ---------------------------------------------------------------------------
# elspeth-25f7b757e7 A3(d) — every producer pairing raw_content=="" with
# composed content publishes a shape the recognizer accepts.
# ---------------------------------------------------------------------------


class TestEmptyRawProducersPublishCanonicalShapes:
    """Server-side counterpart of the frontend split-point pin.

    ``visible_message_segments`` fails closed: an empty-raw result whose
    content is NOT a canonical shape renders as one untrusted
    ``AssistantTextSegment`` — backend copy attributed to the model
    (elspeth-2ed41f0a4a R2). The recognizer side is structurally guarded (the
    bare-suffix and wrapped-template completeness gates); this pins the
    PRODUCER side: each empty-raw publication site must actually compose one
    of those canonical shapes, so a producer edit that drifts a byte cannot
    ship silently demoted.
    """

    @staticmethod
    def _assert_canonical(result: Any) -> None:
        segments = no_tool_policy.visible_message_segments(
            content=result.message,
            raw_content=result.raw_assistant_content,
        )
        assert segments != (no_tool_policy.AssistantTextSegment(result.message),), (
            "empty-raw producer published a non-canonical shape — it renders as model-attributed text"
        )
        for segment in segments:
            trusted = isinstance(segment, no_tool_policy.TrustedSystemNoticeSegment)
            assert trusted or segment.content.startswith("Cause: ")

    def test_replacer_handoff_signoff_failed_site(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        result = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_signoff_failed_handoff_result(),
                raw_assistant_content=None,
            ),
            session_id="sess-12",
        )
        assert result.raw_assistant_content == ""
        self._assert_canonical(result)

    def test_replacer_preflight_failure_site(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        result = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_structural_failure_result(),
                raw_assistant_content="prose",
            ),
            session_id="sess-13",
        )
        assert result.raw_assistant_content == ""
        self._assert_canonical(result)

    def test_replacer_signoff_pending_site(self, publication_spy: _RecordingPublicationTelemetry) -> None:
        from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult

        completion_withheld = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=False,
                blockers=[],
            ),
        )
        result = _replace_advisor_repair_public_result(
            ComposerResult(
                # message must extend raw_assistant_content: the protocol
                # validator rejects an unsynthesized pair on a non-failed
                # preflight, and real replacer inputs carry augmented prose.
                message="prose with a completion note",
                state=_empty_state(),
                runtime_preflight=completion_withheld,
                raw_assistant_content="prose",
            ),
            session_id="sess-14",
        )
        assert result.raw_assistant_content == ""
        self._assert_canonical(result)

    @pytest.mark.parametrize(
        ("reason", "preflight"),
        [
            ("flagged_final_pass", None),
            ("flagged_final_pass", "valid"),
            ("flagged_final_pass", "handoff"),
            ("flagged_final_pass", "red"),
            ("flagged_unrepairable", None),
            ("flagged_unrepairable", "valid"),
            # Fix round 1: the two cells omitted from the first matrix were
            # exactly the two broken ones (N1) — the full reason x shape
            # product is now pinned.
            ("flagged_unrepairable", "handoff"),
            ("flagged_unrepairable", "red"),
        ],
        ids=[
            "absent",
            "green",
            "handoff",
            "red",
            "unrepairable_absent",
            "unrepairable_green",
            "unrepairable_handoff",
            "unrepairable_red",
        ],
    )
    def test_blocked_terminal_site(self, publication_spy: _RecordingPublicationTelemetry, reason: str, preflight: str | None) -> None:
        from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

        shapes = {
            None: None,
            "valid": _valid_result(),
            "handoff": _handoff_composer_result().runtime_preflight,
            "red": _structural_failure_result(),
        }
        service = service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        result = _blocked_terminal(
            service,
            runtime_preflight=shapes[preflight],
            session_id="sess-15",
            reason=reason,
        )
        assert result.raw_assistant_content == ""
        self._assert_canonical(result)
