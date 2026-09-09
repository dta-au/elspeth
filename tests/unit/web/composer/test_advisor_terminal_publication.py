"""Advisor terminal publication: branch attribution + withheld-prose disclosure.

Two sibling defects from live session 39578c6f (2026-08-28):

* elspeth-fa18d54eef — a live turn published the pending-handoff "did not
  clear" notice under a journal trail (advisor pass CLEAN, no withheld
  disclosure row) that this tree cannot produce. Which terminal branch
  published which message was UNOBSERVABLE after the fact: every
  advisor-cohort publication site now mints an ``AdvisorTerminalPublication``
  onto its result, the site holding the turn's session write context persists
  it as an ``advisor_terminal_publication_audit`` row, and the
  ``composer.advisor_terminal_publication`` event mirrors that row — so any
  recurrence is attributable from the session's own audit trail.

* elspeth-ff4f0068a4 — the repair cohort replaces the model's prose with
  fixed backend copy, so a turn in which the composer explained that a user
  instruction could not be applied (one served LLM profile; the user asked
  for two) published nothing telling the user that. Every terminal message
  that replaces withheld prose now carries a fixed operator-authored
  disclosure: verify the pipeline before assuming every requested change was
  applied.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import advisor_audit, no_tool_policy
from elspeth.web.composer import service as service_module
from elspeth.web.composer.advisor_audit import ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND, AdvisorTerminalPublication
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import _replace_advisor_repair_public_result
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.sessions.protocol import SessionServiceProtocol

from .test_runtime_preflight_pending_review_verification import (
    _handoff_composer_result,
    _signoff_failed_handoff_result,
    _structural_failure_result,
    _valid_result,
)

_DISCLOSURE = no_tool_policy.ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE


def _empty_state() -> CompositionState:
    return CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=7, sources={})


def _publication(result: ComposerResult) -> AdvisorTerminalPublication:
    """The record a publication site minted onto its result."""
    publication = result.advisor_terminal_publication
    assert publication is not None, "publication site returned a result without naming its branch"
    return publication


def _service() -> Any:
    from tests.unit.web.composer.test_service import _make_settings, _mock_catalog

    return service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())


def _fenced_session(service: Any) -> tuple[str, SessionOperationContext]:
    """A session id plus the COMPOSE operation held over it, with a sessions
    service that records every ``add_message`` (the audit row write)."""
    if service._sessions_service is None:
        service._sessions_service = MagicMock(
            spec=SessionServiceProtocol, add_message=AsyncMock(spec=SessionServiceProtocol.add_message, return_value=None)
        )
    session_id = str(uuid.uuid4())
    context = SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id,
            operation_id=f"advisor-publication-{session_id}",
            lease_token=f"advisor-publication-token-{session_id}",
            operation_epoch=1,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    return session_id, context


def _publication_rows(service: Any) -> list[dict[str, Any]]:
    rows = []
    for call in service._sessions_service.add_message.await_args_list:
        tool_calls = call.kwargs.get("tool_calls")
        if call.args[1] == "audit" and tool_calls and tool_calls[0]["_kind"] == ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND:
            rows.append(tool_calls[0]["publication"])
    return rows


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
            no_tool_policy._ADVISOR_SIGNOFF_FLAGGED_RED_FOOTER,
            no_tool_policy._ADVISOR_SIGNOFF_UNRENDERED_RED_FOOTER,
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
            "signoff_flagged_red_footer",
            "signoff_unrendered_red_footer",
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
# elspeth-fa18d54eef — every publication branch names itself on the result it
# publishes; the record is what the audit row and the event carry.
# ---------------------------------------------------------------------------


class TestRepairPublicationBranchAttribution:
    def test_unverified_branch(self) -> None:
        published = _replace_advisor_repair_public_result(ComposerResult(message="prose", state=_empty_state(), runtime_preflight=None))
        assert _publication(published).branch == "repair_unverified"
        assert _publication(published).preflight_shape == "absent"

    def test_success_branch(self) -> None:
        published = _replace_advisor_repair_public_result(
            ComposerResult(message="prose", state=_empty_state(), runtime_preflight=_valid_result())
        )
        assert _publication(published).branch == "repair_success"
        assert _publication(published).preflight_shape == "green"

    def test_bare_review_branch(self) -> None:
        published = _replace_advisor_repair_public_result(_handoff_composer_result())
        assert _publication(published).branch == "repair_review"
        assert _publication(published).preflight_shape == "pending_handoff"

    def test_review_with_findings_branch(self) -> None:
        published = _replace_advisor_repair_public_result(
            _handoff_composer_result(),
            outstanding_findings=_structural_failure_result(),
        )
        assert _publication(published).branch == "repair_review_with_findings"

    def test_signoff_failed_handoff_branch(self) -> None:
        published = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_signoff_failed_handoff_result(),
                raw_assistant_content=None,
            )
        )
        assert _publication(published).branch == "repair_handoff_signoff_failed"

    def test_preflight_failure_branch(self) -> None:
        published = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_structural_failure_result(),
                raw_assistant_content="prose",
            )
        )
        assert _publication(published).branch == "repair_preflight_failure"
        assert _publication(published).preflight_shape == "red"

    def test_replacer_is_pure(self) -> None:
        """The replacer writes nothing: it needs no session, no store and no
        fence, and every repair branch passes ``reason=None``."""
        published = _replace_advisor_repair_public_result(_handoff_composer_result())
        assert _publication(published).reason is None
        assert published.advisor_terminal_published is False


class TestQualifiedReplacerPersistsThePublication:
    """``_qualified_advisor_repair_public_result`` holds the turn's session
    write context: it persists the branch the replacer chose as an
    ``advisor_terminal_publication_audit`` row, then mirrors it to telemetry."""

    @pytest.mark.asyncio
    async def test_repair_branch_writes_one_audit_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _service()
        session_id, context = _fenced_session(service)
        mirrored: list[dict[str, Any]] = []
        monkeypatch.setattr(advisor_audit, "record_advisor_terminal_publication", lambda **kw: mirrored.append(kw))

        published = await service._qualified_advisor_repair_public_result(
            ComposerResult(message="prose", state=_empty_state(), runtime_preflight=_valid_result()),
            user_id="alice",
            session_id=session_id,
            session_operation_context=context,
            cache=service._new_runtime_preflight_cache(),
            initial_version=0,
            session_scope="s1",
        )

        assert _publication(published).branch == "repair_success"
        assert _publication_rows(service) == [_publication(published).to_dict()]
        assert mirrored == [{"session_id": session_id, **_publication(published).to_dict()}]

    @pytest.mark.asyncio
    async def test_blocked_terminal_passes_through_without_a_second_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """elspeth-2ae50afcd1: the gate already persisted ``terminal_block``."""
        service = _service()
        session_id, context = _fenced_session(service)
        mirrored: list[dict[str, Any]] = []
        monkeypatch.setattr(advisor_audit, "record_advisor_terminal_publication", lambda **kw: mirrored.append(kw))
        blocked = _blocked_terminal(service, runtime_preflight=None)

        published = await service._qualified_advisor_repair_public_result(
            blocked,
            user_id="alice",
            session_id=session_id,
            session_operation_context=context,
            cache=service._new_runtime_preflight_cache(),
            initial_version=0,
            session_scope="s1",
        )

        assert published == blocked
        assert _publication_rows(service) == []
        assert mirrored == []

    @pytest.mark.asyncio
    async def test_session_without_its_fence_is_refused_before_publishing(self) -> None:
        service = _service()
        session_id, _context = _fenced_session(service)
        with pytest.raises(TypeError, match="requires the turn's session_operation_context"):
            await service._qualified_advisor_repair_public_result(
                ComposerResult(message="prose", state=_empty_state(), runtime_preflight=_valid_result()),
                user_id="alice",
                session_id=session_id,
                session_operation_context=None,
                cache=service._new_runtime_preflight_cache(),
                initial_version=0,
                session_scope="s1",
            )
        assert _publication_rows(service) == []


def _blocked_terminal(
    service: Any,
    *,
    runtime_preflight: Any,
    findings_backend_authored: bool = False,
    reason: str = "flagged_final_pass",
) -> Any:
    from elspeth.web.composer.service import AdvisorCheckpointVerdict
    from elspeth.web.composer.tool_batch import BufferingRecorder

    # The verdict shape must match the reason the way the gate produces it:
    # unavailable/malformed are ok=False failure classes (the red chat arm
    # branches on ``verdict.ok``, elspeth-b61894d93d); every flagged reason
    # is ok=True, blocking=True.
    if reason in {"unavailable", "malformed"}:
        verdict = AdvisorCheckpointVerdict(
            ok=False,
            blocking=False,
            findings_text="advisor verdict could not be obtained",
            failure_class=reason,  # type: ignore[arg-type]
        )
    else:
        verdict = AdvisorCheckpointVerdict(
            ok=True,
            blocking=True,
            findings_text="FLAGGED: still wrong",
            findings_backend_authored=findings_backend_authored,
        )
    return service._advisor_blocked_result(
        reason=reason,
        verdict=verdict,
        state=_empty_state(),
        assistant_message=None,
        recorder=BufferingRecorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=runtime_preflight,
        outstanding_findings=None,
    )


class TestBlockedTerminalBranchAttribution:
    def test_blocked_terminal_names_reason_and_shape(self) -> None:
        blocked = _blocked_terminal(_service(), runtime_preflight=None)
        assert _publication(blocked).branch == "terminal_block"
        assert _publication(blocked).reason == "flagged_final_pass"
        assert _publication(blocked).preflight_shape == "absent"

    def test_blocked_handoff_terminal_reports_handoff_shape(self) -> None:
        blocked = _blocked_terminal(_service(), runtime_preflight=_handoff_composer_result().runtime_preflight)
        assert _publication(blocked).branch == "terminal_block"
        assert _publication(blocked).preflight_shape == "pending_handoff"

    def test_blocked_unrepairable_handoff_terminal_reports_reason_and_shape(self) -> None:
        """Fix round 1: the reason x shape cell the first matrix omitted."""
        blocked = _blocked_terminal(
            _service(),
            runtime_preflight=_handoff_composer_result().runtime_preflight,
            reason="flagged_unrepairable",
            findings_backend_authored=True,
        )
        assert _publication(blocked).branch == "terminal_block"
        assert _publication(blocked).reason == "flagged_unrepairable"
        assert _publication(blocked).preflight_shape == "pending_handoff"
        assert _publication(blocked).findings_backend_authored is True

    def test_blocked_terminal_reason_is_a_closed_vocabulary(self) -> None:
        with pytest.raises(AuditIntegrityError):
            _blocked_terminal(_service(), runtime_preflight=None, reason="because")


class TestPublicationFindingsProvenance:
    """elspeth-25f7b757e7 (A2): the publication record says whether its wording
    embeds the backend-authored pre-scan finding. Only the blocked terminal can
    carry True (it is the only publication whose wording rides the verdict);
    every repair-cohort branch publishes fixed copy with no finding at all."""

    def test_blocked_terminal_reports_backend_authored_findings(self) -> None:
        blocked = _blocked_terminal(_service(), runtime_preflight=None, findings_backend_authored=True)
        assert _publication(blocked).findings_backend_authored is True

    def test_blocked_terminal_reports_model_findings_as_not_backend_authored(self) -> None:
        blocked = _blocked_terminal(_service(), runtime_preflight=None)
        assert _publication(blocked).findings_backend_authored is False

    def test_replacer_branches_report_no_backend_finding(self) -> None:
        published = _replace_advisor_repair_public_result(ComposerResult(message="prose", state=_empty_state(), runtime_preflight=None))
        assert _publication(published).findings_backend_authored is False


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
    ``_advisor_blocked_result`` already names ``terminal_block`` on the result
    and the gate persists it; the replacer must pass such a result through
    untouched."""

    def test_absent_preflight_blocked_terminal_keeps_its_one_record(self) -> None:
        blocked = _blocked_terminal(_service(), runtime_preflight=None)
        published = _replace_advisor_repair_public_result(blocked)
        assert published == blocked
        assert _publication(published) is _publication(blocked)
        assert _publication(published).preflight_shape == "absent"

    def test_handoff_preflight_blocked_terminal_keeps_its_one_record(self) -> None:
        blocked = _blocked_terminal(_service(), runtime_preflight=_handoff_composer_result().runtime_preflight)
        published = _replace_advisor_repair_public_result(blocked)
        assert published == blocked
        assert _publication(published).branch == "terminal_block"

    def test_blocked_terminal_carries_the_published_marker(self) -> None:
        """The pass-through derives from the producer's own positive proof,
        not from re-parsing the synthesized preflight shape (a raw-prose
        result with a genuine signoff-failed preflight must still be
        replaced — pinned by ``test_real_advisor_failure_still_publishes_the_notice``).
        The marker is derived from the record: only ``terminal_block`` sets it."""
        blocked = _blocked_terminal(_service(), runtime_preflight=None)
        assert blocked.advisor_terminal_published is True
        assert ComposerResult(message="prose", state=_empty_state()).advisor_terminal_published is False
        repaired = _replace_advisor_repair_public_result(ComposerResult(message="prose", state=_empty_state(), runtime_preflight=None))
        assert repaired.advisor_terminal_published is False


class TestTelemetryHelperShape:
    """The event is the telemetry MIRROR of an already-committed audit row: an
    exporter failure is acknowledged on the last-resort channel, never
    swallowed and never allowed to displace the committed outcome; registered
    Tier-1 integrity failures still escape."""

    class _Logger:
        def __init__(self, *, info_failure: Exception | None = None, error_failure: Exception | None = None) -> None:
            self.info_calls: list[dict[str, Any]] = []
            self.error_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
            self._info_failure = info_failure
            self._error_failure = error_failure

        def info(self, *a: Any, **k: Any) -> None:
            if self._info_failure is not None:
                raise self._info_failure
            self.info_calls.append(k)

        def error(self, *a: Any, **k: Any) -> None:
            if self._error_failure is not None:
                raise self._error_failure
            self.error_calls.append((a, k))

    @staticmethod
    def _emit(telemetry: Any) -> None:
        telemetry.record_advisor_terminal_publication(
            session_id="sess-8",
            branch="repair_review",
            reason=None,
            preflight_shape="pending_handoff",
            findings_backend_authored=False,
        )

    def test_event_sink_failure_is_acknowledged_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer import advisor_checkpoint_telemetry as telemetry

        logger = self._Logger(info_failure=RuntimeError("exporter outage"))
        monkeypatch.setattr(telemetry, "slog", logger)
        self._emit(telemetry)
        assert logger.error_calls == [
            (("composer.advisor_telemetry_failed",), {"operation": "terminal_publication_event", "error_type": "RuntimeError"})
        ]
        assert "exporter outage" not in repr(logger.error_calls)

    def test_counter_failure_is_acknowledged_and_independent_of_the_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer import advisor_checkpoint_telemetry as telemetry

        class _ExplodingCounter:
            def add(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("meter outage")

        logger = self._Logger()
        monkeypatch.setattr(telemetry, "slog", logger)
        monkeypatch.setattr(telemetry, "_ADVISOR_TERMINAL_PUBLICATIONS_COUNTER", _ExplodingCounter())
        self._emit(telemetry)
        assert [call["branch"] for call in logger.info_calls] == ["repair_review"]
        assert logger.error_calls == [
            (("composer.advisor_telemetry_failed",), {"operation": "terminal_publication_counter", "error_type": "RuntimeError"})
        ]

    def test_last_resort_logger_failure_cannot_displace_the_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer import advisor_checkpoint_telemetry as telemetry

        logger = self._Logger(info_failure=RuntimeError("exporter outage"), error_failure=RuntimeError("logger outage"))
        monkeypatch.setattr(telemetry, "slog", logger)
        self._emit(telemetry)

    def test_tier1_failure_escapes_from_both_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer import advisor_checkpoint_telemetry as telemetry

        monkeypatch.setattr(telemetry, "slog", self._Logger(info_failure=AuditIntegrityError("integrity")))
        with pytest.raises(AuditIntegrityError):
            self._emit(telemetry)
        monkeypatch.setattr(
            telemetry, "slog", self._Logger(info_failure=RuntimeError("exporter outage"), error_failure=AuditIntegrityError("integrity"))
        )
        with pytest.raises(AuditIntegrityError):
            self._emit(telemetry)


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

    def test_clean_fallthrough_over_real_handoff_publishes_review_message(self) -> None:
        result = ComposerResult(
            message="prose",
            state=_empty_state(),
            runtime_preflight=_producer_honest_handoff_result(),
            raw_assistant_content=None,
        )
        published = _replace_advisor_repair_public_result(result)
        assert published.message == no_tool_policy.ADVISOR_REPAIR_REVIEW_PUBLIC_MESSAGE
        assert "did not clear" not in published.message
        assert _publication(published).branch == "repair_review"

    def test_real_advisor_failure_still_publishes_the_notice(self) -> None:
        """A check the advisor path actually built (outcome_code=None) keeps
        the blocked wording — the fix must not widen into ignoring genuine
        verdicts."""
        published = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_signoff_failed_handoff_result(),
                raw_assistant_content=None,
            )
        )
        assert no_tool_policy._ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE in published.message
        assert _publication(published).branch == "repair_handoff_signoff_failed"

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

    def test_replacer_handoff_signoff_failed_site(self) -> None:
        result = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_signoff_failed_handoff_result(),
                raw_assistant_content=None,
            )
        )
        assert result.raw_assistant_content == ""
        self._assert_canonical(result)

    def test_replacer_preflight_failure_site(self) -> None:
        result = _replace_advisor_repair_public_result(
            ComposerResult(
                message="prose",
                state=_empty_state(),
                runtime_preflight=_structural_failure_result(),
                raw_assistant_content="prose",
            )
        )
        assert result.raw_assistant_content == ""
        self._assert_canonical(result)

    def test_replacer_signoff_pending_site(self) -> None:
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
            )
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
            # elspeth-b61894d93d: the unrendered-verdict red shapes.
            ("unavailable", "red"),
            ("malformed", "red"),
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
            "unavailable_red",
            "malformed_red",
        ],
    )
    def test_blocked_terminal_site(self, reason: str, preflight: str | None) -> None:
        shapes = {
            None: None,
            "valid": _valid_result(),
            "handoff": _handoff_composer_result().runtime_preflight,
            "red": _structural_failure_result(),
        }
        result = _blocked_terminal(
            _service(),
            runtime_preflight=shapes[preflight],
            reason=reason,
        )
        assert result.raw_assistant_content == ""
        self._assert_canonical(result)
