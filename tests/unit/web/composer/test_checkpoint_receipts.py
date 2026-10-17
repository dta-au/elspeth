"""Checkpoint facts preserve provenance without granting publication authority."""

import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import no_tool_policy as policy
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.protocol import ComposerResult, PipelineCommitIntent
from elspeth.web.composer.service import ComposerServiceImpl, _announce_staged_review_handoff
from elspeth.web.composer.state import OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import _composer_history_content, composer_turn_end_assistant_row
from tests.unit.web.composer._helpers import _make_settings, _mock_catalog
from tests.unit.web.composer.test_no_tool_policy_segments import (
    _BARE_SUFFIX_ROUND_TRIP_CASES,
    _WRAPPED_TEMPLATE_ROUND_TRIP_CASES,
    _invalid_preflight,
)
from tests.unit.web.composer.test_turn_effects import SID, _evidence, _state, _witness


def _provider_history(content: str, raw: str | None) -> str:
    row = ChatMessageRecord(
        id=uuid4(),
        session_id=UUID(SID),
        role="assistant",
        content=content,
        raw_content=raw,
        created_at=datetime.now(UTC),
        writer_principal="compose_loop",
    )
    return _composer_history_content(row)


@pytest.mark.parametrize("mode", ["zero", "change", "identity", "revert", "unavailable", "sessionless", "no-operation"])
def test_saved_scope_reports_only_checkpoint_evidence(mode: str) -> None:
    evidence = _evidence()
    if mode in ("change", "identity", "revert", "unavailable"):
        state = _state("Private draft label" if mode != "identity" else None, version=2)
        evidence.accept_checkpoint(_witness(state), expected_state=state, tool_call_ids=("save",), assistant_message_id=str(uuid4()))
    if mode == "revert":
        state = _state(version=3)
        evidence.accept_checkpoint(_witness(state), expected_state=state, tool_call_ids=("revert",), assistant_message_id=str(uuid4()))
    if mode == "unavailable":
        evidence.mark_verification_unavailable()
    if mode in ("sessionless", "no-operation"):
        evidence = TurnEvidence(
            session_id=None if mode == "sessionless" else SID,
            session_operation_context=None,
            request_id="receipt",
            request="Explain",
            initial_state_id=None,
            initial_state=_state(),
        )
    text = evidence.render_saved_scope()
    message = policy.compose_checkpoint_receipt_message("Status explanation.", raw_content="Status explanation.", evidence=evidence)
    assert policy.visible_message_segments(content=message, raw_content="Status explanation.") == (
        policy.AssistantTextSegment("Status explanation."),
        policy.TrustedSystemNoticeSegment(f"Checkpoint save status: {text} Checkpoint evidence does not establish task completion."),
    )
    assert "Private draft label" not in text
    assert "metadata.name" not in text
    assert "This request saved" not in text
    if mode in ("sessionless", "no-operation"):
        assert text.startswith("No verified persistence receipt is available")
        assert ("sessionless" in text) == (mode == "sessionless")
    elif mode == "unavailable":
        assert text == "Checkpoint evidence records 1 committed draft revisions; saved-change verification is unavailable."
    else:
        assert text.startswith(f"Verified checkpoint evidence covers {evidence.effects.saved_revision_count} saved draft revisions.")
        assert ("There are net authored changes" in text) == (mode == "change")


def test_closed_receipt_region_is_recognized_at_exact_raw_boundary() -> None:
    raw = "Here is the explanation."
    notice = (
        "Checkpoint save status: Verified checkpoint evidence covers 0 saved draft revisions. "
        "There is no net authored change from the starting draft. Checkpoint evidence does not establish task completion."
    )
    content = raw + "\n\n---\n\n[ELSPETH-SYSTEM] " + notice
    assert policy.visible_message_segments(content=content, raw_content=raw) == (
        policy.AssistantTextSegment(raw),
        policy.TrustedSystemNoticeSegment(notice),
    )


@pytest.mark.parametrize("base", ["bare", "clarification", "handoff", "pending", "legacy"])
@pytest.mark.parametrize("raw", ["Explanation text.", ""])
def test_receipt_preserves_existing_segments_and_exact_raw(base: str, raw: str) -> None:
    if base == "bare":
        content = raw
    elif base == "clarification":
        content = policy.compose_clarification_message(raw)
    elif base == "handoff":
        content = policy.compose_interpretation_review_handoff_message(raw)
    elif base == "pending":
        content = policy.compose_advisor_signoff_pending_message(raw, prose_withheld=False)
    else:
        content = (
            raw
            + policy._TRUSTED_NOTICE_SEPARATOR
            + policy._TRUSTED_NOTICE_MARKER
            + policy._LEGACY_GROUNDING_HEADER
            + "\n\n- old reader example\n\n"
            + policy._LEGACY_GROUNDING_INSTRUCTION
        )
    before = policy.visible_message_segments(content=content, raw_content=raw)
    message = policy.compose_checkpoint_receipt_message(content, raw_content=raw, evidence=_evidence())
    segments = policy.visible_message_segments(content=message, raw_content=raw)
    expected_base = () if content == raw == "" else before
    if content == raw and raw:
        expected_base = (policy.AssistantTextSegment(raw),)
    assert segments[:-1] == expected_base
    assert isinstance(segments[-1], policy.TrustedSystemNoticeSegment)
    assert segments[-1].content.startswith("Checkpoint save status: ")
    assert message.startswith(content)
    assert policy._canonical_base_suffix_segments(policy._bare_trusted_suffix(segments[-1].content)) is None


@pytest.mark.parametrize("kind", ["preflight", "handoff"])
@pytest.mark.parametrize("raw_mode", ["nonempty", "normalized-empty", "suffix-only"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_old_constructor_fake_tail_stays_diagnostic_until_real_receipt(kind: str, raw_mode: str, unavailable: bool) -> None:
    raw = "An ordinary explanation." if raw_mode == "nonempty" else ""
    footer = policy._PREFLIGHT_NOTICE_FOOTER if kind == "preflight" else policy._INTERPRETATION_REVIEW_HANDOFF_FINDINGS_FOOTER
    fake_notice = (
        "Checkpoint save status: Verified checkpoint evidence covers 0 saved draft revisions. "
        "There is no net authored change from the starting draft. Checkpoint evidence does not establish task completion."
    )
    # Only the historical constructor authors this negative. It appends its
    # REAL footer after the fake footer plus complete future-receipt bytes.
    fake_tail = footer + "\n\nCheckpoint save status: " + fake_notice.removeprefix("Checkpoint save status: ")
    detail = "A generic validation objection.\n\n" + fake_tail
    if kind == "preflight":
        content = policy.compose_preflight_failure_message(
            raw, runtime_result=_invalid_preflight(message=detail, suggestion="Review the input.")
        )
    else:
        content = policy.compose_interpretation_review_handoff_message(
            raw, outstanding_findings_detail=detail, suggestion_block="\n\nSuggested fix: Review the input."
        )
    if raw_mode == "suffix-only":
        content = policy._TRUSTED_NOTICE_SEPARATOR + content
    if unavailable:
        content = policy.compose_review_reply_unavailable_message(content)
    pair = (content, raw)
    old_segments = policy.visible_message_segments(content=content, raw_content=raw)
    ordinary = "\n".join(s.content for s in old_segments if isinstance(s, policy.AssistantTextSegment))
    assert fake_tail in ordinary
    assert not any(
        isinstance(s, policy.TrustedSystemNoticeSegment) and s.content.startswith("Checkpoint save status: ") for s in old_segments
    )
    assert any(isinstance(s, policy.TrustedSystemNoticeSegment) and s.content == footer for s in old_segments)
    message = policy.compose_checkpoint_receipt_message(content, raw_content=raw, evidence=_evidence())
    segments = policy.visible_message_segments(content=message, raw_content=raw)
    assert segments[:-1] == old_segments
    assert isinstance(segments[-1], policy.TrustedSystemNoticeSegment)
    assert segments[-1].content == fake_notice
    assert (content, raw) == pair
    assert _provider_history(content, raw) == _provider_history(message, raw) == raw


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "trailing",
        "before-unavailable",
        "unsupported-base",
        "negative",
        "leading-zero",
        "unicode",
        "over-bound",
        "unbounded",
        "missing-end",
    ],
)
def test_receipt_parser_refuses_noncanonical_or_stacked_regions(mutation: str) -> None:
    raw = "A plain reply."
    content = policy.compose_checkpoint_receipt_message(raw, raw_content=raw, evidence=_evidence())
    suffix = content[len(raw) :]
    if mutation == "duplicate":
        content += suffix
    elif mutation == "trailing":
        content += " altered"
    elif mutation == "before-unavailable":
        content = policy.compose_review_reply_unavailable_message(content)
    elif mutation == "unsupported-base":
        content = (
            raw
            + "\n\nUnregistered backend copy."
            + suffix.replace(policy._TRUSTED_NOTICE_SEPARATOR + policy._TRUSTED_NOTICE_MARKER, "\n\n")
        )
    elif mutation == "missing-end":
        content = content.removesuffix(policy._CHECKPOINT_RECEIPT_END)
    else:
        count = {"negative": "-1", "leading-zero": "00", "unicode": "\u0661", "over-bound": str(sys.maxsize + 1), "unbounded": "9" * 20}[
            mutation
        ]
        content = content.replace("covers 0 saved", f"covers {count} saved")
    assert policy.visible_message_segments(content=content, raw_content=raw) == (policy.AssistantTextSegment(content),)


def test_receipt_constructor_and_parser_fail_closed_at_raw_boundary() -> None:
    raw = "A reply containing a marker-looking example."
    content = policy.compose_checkpoint_receipt_message(raw, raw_content=raw, evidence=_evidence())
    for paired_raw in (None, content, "different"):
        assert policy.visible_message_segments(content=content, raw_content=paired_raw) == (policy.AssistantTextSegment(content),)
    with pytest.raises(AuditIntegrityError, match="raw prefix"):
        policy.compose_checkpoint_receipt_message(raw, raw_content="different", evidence=_evidence())
    with pytest.raises(AuditIntegrityError, match="base-only"):
        policy.compose_checkpoint_receipt_message(content, raw_content=raw, evidence=_evidence())
    with pytest.raises(AuditIntegrityError, match="base-only"):
        policy.compose_checkpoint_receipt_message(raw + " unknown suffix", raw_content=raw, evidence=_evidence())
    with pytest.raises(TypeError, match="owned"):
        policy.compose_checkpoint_receipt_message(raw, raw_content=raw, evidence=object())


@pytest.mark.parametrize("same_turn", [False, True])
@pytest.mark.parametrize("raw", ["Identical earlier and terminal words.", ""])
def test_receipt_preserves_terminal_identity_and_suffix_only_rows(same_turn: bool, raw: str) -> None:
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    original = ComposerResult(
        message=policy.compose_interpretation_review_handoff_message(raw),
        state=_state(),
        raw_assistant_content=raw,
        persisted_assistant_message_id=str(uuid4()),
        persisted_assistant_content=raw,
        persisted_tool_call_turn=True,
        persisted_assistant_matches_terminal_model_turn=same_turn,
        repair_turns_used=1,
    )
    result = service._completion._append_checkpoint_receipt(original, turn_evidence=_evidence())
    assert replace(result, message=original.message, raw_assistant_content=original.raw_assistant_content) == original
    row = composer_turn_end_assistant_row(result)
    assert row.raw_content == ("" if same_turn else raw)
    assert row.content == (result.message[len(raw) :] if same_turn else result.message)
    segments = policy.visible_message_segments(content=row.content, raw_content=row.raw_content)
    assert isinstance(segments[-1], policy.TrustedSystemNoticeSegment)
    assert segments[-1].content.startswith("Checkpoint save status: ")


def test_receipt_keeps_authorized_backend_copy_and_excludes_commit_intent() -> None:
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    original = ComposerResult(message=policy.ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE, state=_state())
    result = service._completion._append_checkpoint_receipt(original, turn_evidence=_evidence())
    assert result.raw_assistant_content == original.message
    assert result.message.startswith(original.message)
    intent_result = replace(original, pipeline_commit_intent=PipelineCommitIntent(proposal_id=uuid4(), draft_hash="a" * 64))
    assert service._completion._append_checkpoint_receipt(intent_result, turn_evidence=_evidence()) is intent_result
    recovery = replace(
        original,
        graph_recovery_intent=GraphRecoveryIntent(action="park", message="Keep this draft.", park_id=None, tool_call_id="recovery"),
    )
    assert service._completion._append_checkpoint_receipt(recovery, turn_evidence=_evidence()) is recovery
    assert _provider_history(result.message, result.raw_assistant_content) == original.message


@pytest.mark.parametrize(
    "producer", [policy.compose_advisor_signoff_unrendered_pending_message, policy.compose_advisor_signoff_unrendered_unverified_message]
)
@pytest.mark.parametrize("raw", ["A retained reply.", ""])
def test_budget_fixed_copy_has_a_canonical_base_before_receipt(producer, raw: str) -> None:
    notice = (
        "The reply was withheld because the completion review could not run within the existing request limits. "
        "Saved draft progress remains available; continue in a new message."
    )
    old_content = raw + notice
    assert policy.visible_message_segments(content=old_content, raw_content=raw) == (policy.AssistantTextSegment(old_content),)
    content = producer(raw, failure_class="budget_exhausted")
    base_segments = policy.visible_message_segments(content=content, raw_content=raw)
    assert isinstance(base_segments[-1], policy.TrustedSystemNoticeSegment)
    assert base_segments[-1].content == notice
    message = policy.compose_checkpoint_receipt_message(content, raw_content=raw, evidence=_evidence())
    segments = policy.visible_message_segments(content=message, raw_content=raw)
    assert segments[:-1] == base_segments
    assert isinstance(segments[-1], policy.TrustedSystemNoticeSegment)
    assert message.startswith(content)
    assert _provider_history(old_content, raw) == _provider_history(content, raw) == _provider_history(message, raw) == raw


def test_late_staged_red_reconstruction_preserves_objection_suggestion_and_final_receipt() -> None:
    raw = "Review the draft choice."
    preflight = _invalid_preflight(message="A generic input contract is inconsistent.", suggestion="Select matching input columns.")
    state = (
        _state()
        .with_source(SourceSpec(plugin="csv", on_success="main", options={}, on_validation_failure="discard"))
        .with_output(OutputSpec(name="main", plugin="csv", options={}, on_write_failure="discard"))
    )
    tail = ComposerResult(
        message=policy.compose_preflight_failure_message(raw, runtime_result=preflight),
        raw_assistant_content=raw,
        runtime_preflight=preflight,
        state=state,
    )
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    # Negative ordering control: this real late reconstruction drops an early
    # receipt, which is why the loop's final owner must decorate afterwards.
    early = service._completion._append_checkpoint_receipt(tail, turn_evidence=_evidence())
    reconstructed = _announce_staged_review_handoff(early, raw)
    assert "Checkpoint save status: " not in reconstructed.message
    assert reconstructed == _announce_staged_review_handoff(tail, raw)
    result = service._completion._append_checkpoint_receipt(reconstructed, turn_evidence=_evidence())
    segments = policy.visible_message_segments(content=result.message, raw_content=raw)
    assert segments[:-1] == policy.visible_message_segments(content=reconstructed.message, raw_content=raw)
    assert "A generic input contract is inconsistent." in result.message
    assert "Suggested fix: Select matching input columns." in result.message
    assert "Interpretation review cards are ready for this pipeline." in result.message
    assert result.raw_assistant_content == raw
    assert isinstance(segments[-1], policy.TrustedSystemNoticeSegment)
    assert segments[-1].content.startswith("Checkpoint save status: ")


def test_receipt_count_bound_and_raw_lookalike_do_not_share_terminal_vocabulary() -> None:
    raw = "An explanatory example."
    content = policy.compose_checkpoint_receipt_message(raw, raw_content=raw, evidence=_evidence())
    bounded = content.replace("covers 0 saved", f"covers {sys.maxsize} saved")
    assert isinstance(policy.visible_message_segments(content=bounded, raw_content=raw)[-1], policy.TrustedSystemNoticeSegment)
    for footer in (
        policy._PREFLIGHT_NOTICE_FOOTER,
        policy._INTERPRETATION_REVIEW_HANDOFF_FINDINGS_FOOTER,
        policy._LEGACY_GROUNDING_INSTRUCTION,
    ):
        assert not bounded.endswith(footer)
    result = policy.compose_checkpoint_receipt_message(content, raw_content=content, evidence=_evidence())
    segments = policy.visible_message_segments(content=result, raw_content=content)
    assert segments[0] == policy.AssistantTextSegment(content)
    assert len(segments) == 2 and isinstance(segments[-1], policy.TrustedSystemNoticeSegment)


@pytest.mark.asyncio
async def test_checkpoint_scope_stays_zero_when_route_later_saves_a_revision(tmp_path: Path) -> None:
    from elspeth.web.sessions.schemas import MessageWithStateResponse
    from tests.unit.web.sessions.test_run_composer_turn import _make_app, _make_composer_mock, _run, _running_job

    app, sessions = _make_app(tmp_path)
    composer = _make_composer_mock()
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Checkpoint scope", "local")
    replies: list[ComposerResult] = []

    async def checkpoint_result(message, history, starting_state, **kwargs):
        evidence = TurnEvidence(
            session_id=str(session.id),
            session_operation_context=kwargs["session_operation_context"],
            request_id="scope-control",
            request=message,
            initial_state_id=None,
            initial_state=starting_state,
        )
        raw = "Here is the updated draft."
        result = ComposerResult(
            message=policy.compose_checkpoint_receipt_message(raw, raw_content=raw, evidence=evidence),
            raw_assistant_content=raw,
            state=replace(starting_state, metadata=PipelineMetadata(name="Route projection"), version=starting_state.version + 1),
        )
        replies.append(result)
        return result

    # Existing route harness models only the Composer boundary; real adopted
    # authority, post_compose persistence and terminal response run underneath.
    composer.compose.side_effect = checkpoint_result
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Update the draft name.") as job:
        record = await _run(app, job)
    assert record.status == "completed" and record.result_json is not None
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert response.message.content == replies[0].message
    assert response.message.raw_content is None  # Existing public response privacy contract.
    assert [segment.kind for segment in response.message.segments] == ["text", "trusted_system_notice"]
    assert response.message.segments[0].content == replies[0].raw_assistant_content
    assert "Verified checkpoint evidence covers 0 saved draft revisions." in response.message.content
    versions = await sessions.get_state_versions(session.id)
    assert len(versions) == 1
    current = await sessions.get_current_state(session.id)
    assert current is not None
    with sessions._engine.connect() as conn:
        provenance = conn.execute(
            select(composition_states_table.c.provenance).where(composition_states_table.c.id == str(current.id))
        ).scalar_one()
    assert provenance == "post_compose"
    stored = await sessions.get_messages(session.id, limit=None)
    terminal = next(row for row in stored if row.role == "assistant")
    assert (terminal.content, terminal.raw_content) == (replies[0].message, replies[0].raw_assistant_content)
    assert _composer_history_content(terminal) == replies[0].raw_assistant_content


@pytest.mark.parametrize(("suffix", "notice"), _BARE_SUFFIX_ROUND_TRIP_CASES)
@pytest.mark.parametrize("raw", ["A generic explanation.", ""])
def test_receipt_composes_with_every_registered_bare_base(suffix: str, notice: str, raw: str) -> None:
    base = raw + suffix
    message = policy.compose_checkpoint_receipt_message(base, raw_content=raw, evidence=_evidence())
    segments = policy.visible_message_segments(content=message, raw_content=raw)
    assert segments[:-1] == (*((policy.AssistantTextSegment(raw),) if raw else ()), policy.TrustedSystemNoticeSegment(notice))
    assert segments[-1] == policy.TrustedSystemNoticeSegment(
        "Checkpoint save status: Verified checkpoint evidence covers 0 saved draft revisions. "
        "There is no net authored change from the starting draft. Checkpoint evidence does not establish task completion."
    )


@pytest.mark.parametrize(("template", "header", "footer", "format_kwargs"), _WRAPPED_TEMPLATE_ROUND_TRIP_CASES)
@pytest.mark.parametrize("raw", ["A generic explanation.", ""])
@pytest.mark.parametrize("unavailable", [False, True])
def test_receipt_preserves_every_registered_wrapped_base(
    template: str, header: str, footer: str, format_kwargs: dict[str, str], raw: str, unavailable: bool
) -> None:
    base = raw + template.format(**format_kwargs)
    if unavailable:
        base = policy.compose_review_reply_unavailable_message(base)
    before = policy.visible_message_segments(content=base, raw_content=raw)
    assert policy.TrustedSystemNoticeSegment(header) in before
    assert policy.TrustedSystemNoticeSegment(footer) in before
    assert any(isinstance(segment, policy.AssistantTextSegment) and segment.content.startswith("Cause: ") for segment in before)
    message = policy.compose_checkpoint_receipt_message(base, raw_content=raw, evidence=_evidence())
    segments = policy.visible_message_segments(content=message, raw_content=raw)
    assert segments[:-1] == before
    assert isinstance(segments[-1], policy.TrustedSystemNoticeSegment)
    assert segments[-1].content.startswith("Checkpoint save status: ")
