"""The turn-end assistant row must not re-carry already-persisted prose.

elspeth-d581b3da7f. The staged interpretation-review handoff terminates the
tool batch at the successful review call, so the model's last prose IS the
tool-call turn's prose — which ``persist_compose_turn_async`` has already
committed. Both turn-end writers used to persist ``result.message`` verbatim,
so the planner's text landed in the operator's transcript twice (live session
891b7b1e: two rows 99ms apart, the second carrying the trusted notice).

``composer_turn_end_assistant_row`` is the single authority both routes call.
These tests pin its decision table, including the shapes that make a generic
persisted-turn flag or byte equality unsafe, and the segment classification the
split row depends on.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.no_tool_policy import (
    ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE,
    TrustedSystemNoticeSegment,
    compose_interpretation_review_handoff_message,
    visible_message_segments,
)
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.sessions.routes._helpers import composer_turn_end_assistant_row

_PROSE = "Surfacing the review card now."
_HANDOFF_MESSAGE = compose_interpretation_review_handoff_message(_PROSE)
_HANDOFF_SUFFIX = _HANDOFF_MESSAGE[len(_PROSE) :]

_EMPTY_STATE = CompositionState(
    source=None,
    nodes=(),
    edges=(),
    outputs=(),
    metadata=PipelineMetadata(),
    version=1,
)


# A red preflight. Needed only by the exact-duplicate case below: ComposerResult
# independently rejects ``message == raw_assistant_content`` unless preflight
# failed, which is a second reason that shape cannot arise on the happy path.
_FAILED_PREFLIGHT = ValidationResult(
    is_valid=False,
    checks=[],
    errors=[],
    readiness=ValidationReadiness(
        authoring_valid=False,
        execution_ready=False,
        completion_ready=False,
        blockers=[],
    ),
)


def _result(
    *,
    message: str,
    raw_assistant_content: str | None = None,
    persisted_assistant_content: str | None = None,
    persisted_assistant_matches_terminal_model_turn: bool = False,
    runtime_preflight: ValidationResult | None = None,
) -> ComposerResult:
    """A ComposerResult in the shape the compose loop hands the turn-end writer."""
    return ComposerResult(
        message=message,
        state=_EMPTY_STATE,
        runtime_preflight=runtime_preflight,
        raw_assistant_content=raw_assistant_content,
        persisted_assistant_message_id=(None if persisted_assistant_content is None else "assistant-row-1"),
        persisted_assistant_content=persisted_assistant_content,
        persisted_tool_call_turn=persisted_assistant_content is not None,
        persisted_assistant_matches_terminal_model_turn=persisted_assistant_matches_terminal_model_turn,
    )


def test_staged_handoff_row_carries_only_the_backend_suffix() -> None:
    """The defect case: the row holds the notice alone, not the prose again."""
    draft = composer_turn_end_assistant_row(
        _result(
            message=_HANDOFF_MESSAGE,
            raw_assistant_content=_PROSE,
            persisted_assistant_content=_PROSE,
            persisted_assistant_matches_terminal_model_turn=True,
        )
    )

    assert draft.content == _HANDOFF_SUFFIX
    assert _PROSE not in draft.content, "the already-persisted prose must not reach the transcript twice"
    # ``raw_content=""`` is the documented shape for backend chrome: the
    # augmentation-prefix read-path invariant still holds and
    # ``_composer_history_content`` keeps the suffix out of prompt history.
    assert draft.raw_content == ""


def test_split_row_still_publishes_the_notice_as_backend_chrome() -> None:
    """The split must not demote the disclosure into model prose.

    ``visible_message_segments`` mints trusted chrome only for the closed set
    of canonical suffixes. A row whose content did not match would fail closed
    to one ``AssistantTextSegment`` — publishing a backend-authored disclosure
    as if the planner had written it (the elspeth-2ed41f0a4a R2 failure).
    """
    draft = composer_turn_end_assistant_row(
        _result(
            message=_HANDOFF_MESSAGE,
            raw_assistant_content=_PROSE,
            persisted_assistant_content=_PROSE,
            persisted_assistant_matches_terminal_model_turn=True,
        )
    )

    segments = visible_message_segments(content=draft.content, raw_content=draft.raw_content)

    assert len(segments) == 1
    assert type(segments[0]) is TrustedSystemNoticeSegment


def test_text_only_final_turn_keeps_its_prose() -> None:
    """Trap 1: the id points at the EARLIER row while message holds LATER prose.

    ``persisted_assistant_message_id`` is carried across loop iterations but
    ``_persist_turn_audit`` runs only on the tool-dispatch path, so a
    ``[tool call, then text-only final turn]`` sequence — reachable through the
    B-4D-3 last-chance finalize — leaves the pair pointing at the earlier row.
    Stripping there would delete genuine model prose.
    """
    # The later prose deliberately STARTS WITH the earlier row's content. The
    # prefix test alone would strip here and hand the transcript
    # " Here is the finished pipeline." with the model's opening deleted. The
    # explicit same-turn identity remains false because this turn's prose is
    # not what that row holds.
    earlier_row = "Working on it."
    later_prose = f"{earlier_row} Here is the finished pipeline."
    draft = composer_turn_end_assistant_row(
        _result(
            message=later_prose,
            raw_assistant_content=None,
            persisted_assistant_content=earlier_row,
        )
    )

    assert draft.content == later_prose
    assert draft.raw_content is None


def test_later_turn_repeated_prose_keeps_the_full_synthesized_message() -> None:
    """Equal bytes do not prove the persisted row belongs to the terminal turn."""
    repeated_prose = "The pipeline is ready for review."
    synthesized = f"{repeated_prose}\n\nBackend qualification."

    draft = composer_turn_end_assistant_row(
        _result(
            message=synthesized,
            raw_assistant_content=repeated_prose,
            persisted_assistant_content=repeated_prose,
        )
    )

    assert draft.content == synthesized
    assert draft.raw_content == repeated_prose


def test_empty_persisted_content_does_not_swallow_the_message() -> None:
    """A tool-call turn with no prose persists ``""`` — every string starts with it.

    This is why the prefix test alone is not a discriminator: it would strip
    the entire turn-end message against an empty persisted row.
    """
    draft = composer_turn_end_assistant_row(
        _result(
            message="The pipeline is ready.",
            raw_assistant_content=None,
            persisted_assistant_content="",
        )
    )

    assert draft.content == "The pipeline is ready."
    # And the row stays ordinary model prose. A prefix-only predicate would
    # match the empty persisted content and rewrite raw_content to "",
    # which tells ``_composer_history_content`` this whole turn is backend
    # chrome — the model's own prose would vanish from prompt history.
    assert draft.raw_content is None


def test_advisor_repair_row_is_not_mistaken_for_the_turn_prose() -> None:
    """Trap 2: that branch persists a fixed public message, not the turn's prose.

    ``_persist_turn_audit`` substitutes ``ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE``
    with ``raw_content=None`` on the ``advisor_repair_context_introduced``
    branch. The committed row is a different utterance, so the turn-end
    message is not a re-emission of it and must survive whole.
    """
    draft = composer_turn_end_assistant_row(
        _result(
            message="The repaired pipeline is ready.",
            raw_assistant_content=None,
            persisted_assistant_content=ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE,
        )
    )

    assert draft.content == "The repaired pipeline is ready."


def test_turn_without_a_persisted_row_is_untouched() -> None:
    """No mid-loop persist (text-only turn, or no session) — nothing to strip."""
    draft = composer_turn_end_assistant_row(_result(message="Plain answer.", raw_assistant_content=None))

    assert draft.content == "Plain answer."
    assert draft.raw_content is None


def test_exact_duplicate_fails_closed_instead_of_writing_an_empty_row() -> None:
    """No suffix left to persist means the row would duplicate verbatim.

    Unreachable by construction (the staged-handoff branch always appends a
    non-empty canonical suffix), so this pins the fail-closed disposition
    rather than a live path: committing an exact duplicate, or an empty
    bubble, are both worse than surfacing the audit-integrity fault.
    """
    with pytest.raises(AuditIntegrityError, match="byte-identical"):
        composer_turn_end_assistant_row(
            _result(
                message=_PROSE,
                raw_assistant_content=_PROSE,
                persisted_assistant_content=_PROSE,
                persisted_assistant_matches_terminal_model_turn=True,
                runtime_preflight=_FAILED_PREFLIGHT,
            )
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {"persisted_assistant_matches_terminal_model_turn": True},
            "persisted assistant id and content",
        ),
        (
            {
                "persisted_assistant_message_id": "assistant-row-1",
                "persisted_assistant_content": _PROSE,
                "persisted_assistant_matches_terminal_model_turn": True,
            },
            "persisted_tool_call_turn",
        ),
        (
            {
                "raw_assistant_content": "Different terminal prose.",
                "persisted_assistant_message_id": "assistant-row-1",
                "persisted_assistant_content": _PROSE,
                "persisted_tool_call_turn": True,
                "persisted_assistant_matches_terminal_model_turn": True,
            },
            "raw_assistant_content",
        ),
        (
            {
                "raw_assistant_content": "Unrelated persisted prose.",
                "persisted_assistant_message_id": "assistant-row-1",
                "persisted_assistant_content": "Unrelated persisted prose.",
                "persisted_tool_call_turn": True,
                "persisted_assistant_matches_terminal_model_turn": True,
            },
            "message must start",
        ),
    ],
    ids=["no-persisted-pair", "not-tool-call-turn", "raw-mismatch", "prefix-mismatch"],
)
def test_same_turn_identity_requires_jointly_consistent_fields(
    kwargs: dict[str, object],
    match: str,
) -> None:
    """The positive discriminator is one-way proof, so every premise is required."""
    with pytest.raises(ValueError, match=match):
        ComposerResult(
            message=_HANDOFF_MESSAGE,
            state=_EMPTY_STATE,
            **kwargs,
        )


def test_half_threaded_persisted_pair_is_unrepresentable() -> None:
    """The guard that converts a missed threading site into a crash.

    The compose loop threads the id and the content through ~13 sites via
    ``dataclasses.replace``. Both fields are defaulted, so a site that carries
    the id and drops the content would otherwise revert to duplicating in
    silence — this biconditional is what makes that shape impossible to build.
    """
    with pytest.raises(ValueError, match="persisted_assistant_message_id and persisted_assistant_content"):
        ComposerResult(
            message=_HANDOFF_MESSAGE,
            state=_EMPTY_STATE,
            raw_assistant_content=_PROSE,
            persisted_assistant_message_id="assistant-row-1",
        )

    with pytest.raises(ValueError, match="persisted_assistant_message_id and persisted_assistant_content"):
        ComposerResult(
            message=_HANDOFF_MESSAGE,
            state=_EMPTY_STATE,
            raw_assistant_content=_PROSE,
            persisted_assistant_content=_PROSE,
        )
