"""Empty-state blocker disclosure must not depend on sentence packaging.

Issue elspeth-a6a2ed6b1d. The no-mutation empty-state finalize branch
(``no_tool_finalize.finalize_no_tool_response``) appends a
system-attributed suffix naming the concrete blocker that stopped the
build. That disclosure was gated on
``classify_pipeline_mutation_intent(...) is EXPLICIT_MUTATION``.

EXPLICIT_MUTATION is a *packaging* verdict: the closed grammar
fullmatches the whole normalised message from character 0 and treats a
single ``?`` anywhere as a hard disqualifier. So "Build a pipeline
that ... to JSON." discloses while "Build me a pipeline that ... to
JSON." does not, though the two requests are the same request. Measured
over the acceptance corpus (``ops-local/acceptance/intents/*.json``):
14 AMBIGUOUS, 3 CONVERSATIONAL, 1 EXPLICIT_MUTATION — so 17 of 18 real
phrasings silently lost the explanation of why nothing got built.

Disclosure now keys on a dedicated predicate, ``carries_build_action``,
rather than on the intent enum. Gating on ``is not CONVERSATIONAL`` was
tried first and rejected on review: two *packaging* short-circuits reach
AMBIGUOUS before the enum's only content test runs (the 4096-char
ceiling and the unbalanced-quote guard), so an oversize or
unbalanced-quote greeting failed open into disclosure. The named
predicate runs the content test unconditionally, so neither
short-circuit decides disclosure any more.

Turns that carry no build action are still excluded: appending "the
composer did not complete a valid build this turn" to a greeting or a
catalog question would be a misattributed system notice in the other
direction.

Routing is untouched: both routing call sites (``service.py`` surface
selection and the freeform planner gate) test ``is EXPLICIT_MUTATION``
against the same unmodified classifier, which this change does not call
on the disclosure path at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.web.composer.no_tool_policy import (
    PipelineMutationIntentDecision,
    classify_pipeline_mutation_intent,
)
from elspeth.web.composer.service import ComposerServiceImpl

from ._helpers import _empty_state, _make_settings, _mock_catalog

# One request, six surface phrasings. The classifier splits these across
# EXPLICIT_MUTATION and AMBIGUOUS on packaging alone (leading context,
# the article after the verb, the adjective vocabulary, a trailing "?").
_SAME_REQUEST_PHRASINGS: tuple[str, ...] = (
    "Build a pipeline that reads manifest.csv and writes results to JSON.",
    "Build me a pipeline that reads manifest.csv and writes results to JSON.",
    "Build a new pipeline that reads manifest.csv and writes results to JSON.",
    "I have manifest.csv. Build a pipeline that reads manifest.csv and writes results to JSON.",
    "Build a pipeline that reads manifest.csv and writes results to JSON. Thanks!",
    "Can you build a pipeline that reads manifest.csv and writes results to JSON?",
)

_MODEL_PROSE = "I could not settle on a source configuration for manifest.csv, so I stopped before building."


def _failed_set_pipeline_invocation() -> ComposerToolInvocation:
    """A build tool that failed before mutating — the concrete blocker."""
    return ComposerToolInvocation(
        tool_call_id="call_failed",
        tool_name="set_pipeline",
        arguments_canonical=b"{}",
        arguments_hash="0" * 64,
        result_canonical=None,
        result_hash=None,
        status=ComposerToolStatus.ARG_ERROR,
        error_class="ToolArgumentError",
        error_message="source.blob_id is required",
        version_before=1,
        version_after=None,
        started_at=datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC),
        latency_ms=1,
        actor="test",
    )


async def _finalize(user_message: str) -> str:
    """Run the no-tool finalizer on an empty state with a failed build tool."""
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    state = _empty_state()
    result = await service._finalize_no_tool_response(
        content=_MODEL_PROSE,
        state=state,
        initial_version=state.version,
        user_id="user-1",
        session_id=None,
        last_runtime_preflight=None,
        runtime_preflight_cache=service._new_runtime_preflight_cache(),
        session_scope="session:test",
        user_message=user_message,
        tool_invocations=(_failed_set_pipeline_invocation(),),
    )
    return result.message


@pytest.mark.asyncio
async def test_ambiguous_empty_state_request_receives_blocker_explanation() -> None:
    """An AMBIGUOUS build request must be told why its pipeline is empty."""
    message = "Build me a pipeline that reads manifest.csv and writes results to JSON."
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS

    finalized = await _finalize(message)

    assert finalized.startswith(_MODEL_PROSE)
    assert "[ELSPETH-SYSTEM]" in finalized
    assert "The pipeline is still empty" in finalized
    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in finalized


@pytest.mark.asyncio
async def test_explicit_empty_state_request_still_receives_blocker_explanation() -> None:
    """Regression guard: the pre-existing EXPLICIT_MUTATION disclosure is unchanged."""
    message = "Build a pipeline that reads manifest.csv and writes results to JSON."
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.EXPLICIT_MUTATION

    finalized = await _finalize(message)

    assert finalized.startswith(_MODEL_PROSE)
    assert "[ELSPETH-SYSTEM]" in finalized
    assert "The pipeline is still empty" in finalized
    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in finalized


@pytest.mark.asyncio
@pytest.mark.parametrize("message", _SAME_REQUEST_PHRASINGS)
async def test_packaging_variants_of_one_request_disclose_identically(message: str) -> None:
    """Six phrasings of one request get one disclosure outcome.

    This is the defect proper: the phrasings differ only in leading
    context, the article after the verb, an adjective, and a trailing
    "?" — none of which bears on whether the user deserves to be told
    why their pipeline is empty.
    """
    finalized = await _finalize(message)

    assert "The pipeline is still empty" in finalized
    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in finalized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["hello", "what transforms do you support?", "Which sinks can write parquet?"],
)
async def test_conversational_turn_receives_no_build_failure_notice(message: str) -> None:
    """A turn that asked for no build is not told a build failed.

    Gating on structural emptiness alone would attach "the composer did
    not complete a valid build this turn" to greetings and catalog
    questions on any fresh session — a misattributed system notice.
    """
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.CONVERSATIONAL

    finalized = await _finalize(message)

    assert finalized == _MODEL_PROSE
    assert "[ELSPETH-SYSTEM]" not in finalized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "message"),
    [
        # Unbalanced quote: classify_pipeline_mutation_intent returns
        # AMBIGUOUS at no_tool_policy.py:589 before reaching any content
        # test, so an enum-based gate failed open here.
        ("unbalanced_quote", 'Hi, what is a "sink?'),
        # Oversize: same story at no_tool_policy.py:585, the 4096 ceiling.
        ("oversize", "hello " + "x" * 4092),
    ],
)
async def test_packaging_short_circuits_do_not_manufacture_disclosure(label: str, message: str) -> None:
    """Neither packaging early-return in the intent grammar grants disclosure.

    Both of these reach AMBIGUOUS without the classifier ever testing
    for a build action. Disclosure must consult content directly.
    """
    finalized = await _finalize(message)

    assert finalized == _MODEL_PROSE
    assert "[ELSPETH-SYSTEM]" not in finalized


@pytest.mark.asyncio
async def test_oversize_build_request_still_receives_blocker_explanation() -> None:
    """A long build request discloses — the ceiling bounds authority, not disclosure.

    Deliberate call: ``classify_pipeline_mutation_intent`` caps input at
    4096 chars because granting *mutation authority* on unbounded text
    is unsafe. Disclosure grants nothing, and failing closed on length
    would re-create the very withholding defect this issue is about for
    the realistic "here is my 6KB CSV, build me a pipeline that ..."
    shape.
    """
    message = "Here is my data. " + "col_a,col_b\n" * 400 + " Build a pipeline that reads it and writes JSON."
    assert len(message) > 4096

    finalized = await _finalize(message)

    assert "The pipeline is still empty" in finalized
    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in finalized


@pytest.mark.asyncio
async def test_build_request_mentioning_run_failure_still_discloses() -> None:
    """A build request that also discusses failure handling still discloses.

    Regression pin from acceptance intents g07/g07r. Suppressing
    disclosure via ``_REQUEST_REVOCATION_PATTERN`` was tried and
    reverted: that pattern is a bare search, and these intents pair
    "Build me a pipeline that runs Amazon Textract ..." with "... rather
    than have the whole run stop.", which it matches. Suppressing here
    re-creates the withholding defect for a real build request.
    """
    message = (
        "Build me a pipeline that runs Amazon Textract over the documents. "
        "If one of them can't be analysed I'd much rather that document ended "
        "up in a quarantine file than have the whole run stop."
    )

    finalized = await _finalize(message)

    assert "The pipeline is still empty" in finalized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Do not build anything.",
        "How do I route rows to two different files?",
        "I make heavy use of CSV files.",
        "Are you able to run pipelines yourself?",
    ],
)
async def test_known_limitation_bare_verb_draws_notice_on_conversation(message: str) -> None:
    """CHARACTERIZATION OF A DEFECT, NOT DESIRED BEHAVIOUR.

    The verb alternation behind ``carries_build_action`` has no object
    requirement, so these conversational turns draw an empty-state
    notice they have not earned. This is asserted so the cost is
    visible and reviewable rather than buried, and so that whoever
    narrows the predicate sees these flip.

    If you are here because this test failed: that is probably GOOD.
    Confirm the message now correctly gets no notice, then delete the
    case — do not restore the assertion.
    """
    finalized = await _finalize(message)

    assert "The pipeline is still empty" in finalized


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_SAME_REQUEST_PHRASINGS[0], PipelineMutationIntentDecision.EXPLICIT_MUTATION),
        (_SAME_REQUEST_PHRASINGS[1], PipelineMutationIntentDecision.AMBIGUOUS),
        (_SAME_REQUEST_PHRASINGS[2], PipelineMutationIntentDecision.AMBIGUOUS),
        (_SAME_REQUEST_PHRASINGS[3], PipelineMutationIntentDecision.AMBIGUOUS),
        (_SAME_REQUEST_PHRASINGS[4], PipelineMutationIntentDecision.EXPLICIT_MUTATION),
        (_SAME_REQUEST_PHRASINGS[5], PipelineMutationIntentDecision.AMBIGUOUS),
    ],
)
def test_routing_classification_is_unchanged(message: str, expected: PipelineMutationIntentDecision) -> None:
    """Characterization pin on the intent grammar the routing call sites read.

    This does NOT prove the disclosure change left routing alone — it
    imports the classifier directly and never exercises the finalize
    branch, so it passes under any edit confined to
    ``no_tool_finalize``. That proof is structural: disclosure no longer
    calls this function at all.

    Its value is forward-looking. Both routing consumers
    (``service.py`` empty-state surface selection and the freeform
    planner gate) decide on ``classify_pipeline_mutation_intent(message)
    is EXPLICIT_MUTATION``. A future fixer loosening this grammar to
    make AMBIGUOUS phrasings route to the planner will trip these
    verdicts and be told, deliberately, that they are changing routing.
    """
    assert classify_pipeline_mutation_intent(message) is expected
