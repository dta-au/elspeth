"""Empty-state failure disclosure follows actual composition attempts.

Request wording does not manufacture a failed build or erase an unwanted
mutation that the provider really tried.
Semantic completion claims are reviewed by the upstream END gate; this file
tests the finalizer's factual receipt notice and public/history provenance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools import is_blob_store_only_mutation_tool, is_mutation_tool

from ._helpers import _empty_state, _make_settings, _mock_catalog

# One request, six surface phrasings. Disclosure follows tool receipts across
# leading context, articles, adjective vocabulary, and trailing punctuation.
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
        arguments_canonical="{}",
        arguments_hash=hashlib.sha256(b"{}").hexdigest(),
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
        error_category=ToolArgumentErrorCategory.MODEL_VALIDATION,
    )


async def _finalize_result(
    user_message: str,
    *,
    tool_invocations: tuple[ComposerToolInvocation, ...] = (),
    mutation_success_seen: bool = False,
) -> ComposerResult:
    """Run the real finalizer with exactly the supplied current-turn receipts."""
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    state = _empty_state()
    result = await service._completion._finalize_no_tool_response(
        content=_MODEL_PROSE,
        state=state,
        initial_version=state.version,
        user_id="user-1",
        session_id=None,
        last_runtime_preflight=None,
        runtime_preflight_cache=service._preflight.new_cache(),
        session_scope="session:test",
        user_message=user_message,
        tool_invocations=tool_invocations,
        mutation_success_seen=mutation_success_seen,
    )
    assert result.tool_invocations == tool_invocations
    return result


async def _finalize(user_message: str) -> str:
    """Preserve the failed-build controls' actual argument-rejection receipt."""
    result = await _finalize_result(user_message, tool_invocations=(_failed_set_pipeline_invocation(),))
    return result.message


def _successful_receipt(tool_name: str, result: dict[str, object]) -> ComposerToolInvocation:
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return replace(
        _failed_set_pipeline_invocation(),
        tool_call_id=f"call_{tool_name}",
        tool_name=tool_name,
        status=ComposerToolStatus.SUCCESS,
        result_canonical=canonical,
        result_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        error_class=None,
        error_message=None,
        error_category=None,
        version_after=1,
    )


@pytest.mark.asyncio
async def test_personalized_empty_state_request_receives_blocker_explanation() -> None:
    """A failed attempt must explain why the pipeline is empty."""
    message = "Build me a pipeline that reads manifest.csv and writes results to JSON."

    finalized = await _finalize(message)

    assert finalized.startswith(_MODEL_PROSE)
    assert "[ELSPETH-SYSTEM]" in finalized
    assert "The pipeline is still empty" in finalized
    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in finalized


@pytest.mark.asyncio
async def test_explicit_empty_state_request_still_receives_blocker_explanation() -> None:
    """A direct request receives the same factual disclosure."""
    message = "Build a pipeline that reads manifest.csv and writes results to JSON."

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
    result = await _finalize_result(message)
    finalized = result.message

    assert finalized == _MODEL_PROSE
    assert "[ELSPETH-SYSTEM]" not in finalized
    assert result.runtime_preflight is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("unbalanced_quote", 'Hi, what is a "sink?'),
        ("oversize", "hello " + "x" * 4092),
    ],
)
async def test_unusual_request_packaging_does_not_manufacture_disclosure(label: str, message: str) -> None:
    """Quotes and message length cannot supply composition-attempt receipts."""
    result = await _finalize_result(message)
    finalized = result.message

    assert finalized == _MODEL_PROSE
    assert "[ELSPETH-SYSTEM]" not in finalized
    assert result.runtime_preflight is None


@pytest.mark.asyncio
async def test_oversize_build_request_still_receives_blocker_explanation() -> None:
    """An actual failed attempt discloses independently of message length."""
    message = "Here is my data. " + "col_a,col_b\n" * 400 + " Build a pipeline that reads it and writes JSON."
    assert len(message) > 4096

    finalized = await _finalize(message)

    assert "The pipeline is still empty" in finalized
    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in finalized


@pytest.mark.asyncio
async def test_build_request_mentioning_run_failure_still_discloses() -> None:
    """A build request that also discusses failure handling still discloses.

    Failure-handling requirements in a request cannot suppress factual
    disclosure of an actual failed composition attempt.
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
        'What does "build a pipeline" mean?',
    ],
)
async def test_zero_attempt_conversation_does_not_manufacture_build_failure(message: str) -> None:
    result = await _finalize_result(message)

    assert result.message == _MODEL_PROSE
    assert result.runtime_preflight is None
    assert "[ELSPETH-SYSTEM]" not in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["hello", "Do not build anything.", "How do I route rows to two different files?"])
async def test_unwanted_real_mutation_failure_is_disclosed_even_on_conversation(message: str) -> None:
    finalized = await _finalize(message)

    assert "The pipeline is still empty" in finalized
    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in finalized


@pytest.mark.asyncio
@pytest.mark.parametrize("message", _SAME_REQUEST_PHRASINGS)
async def test_zero_attempt_build_request_has_no_fabricated_failure_receipt(message: str) -> None:
    """END owns false-completion review; the finalizer must not invent a tool failure."""
    result = await _finalize_result(message)

    assert result.message == _MODEL_PROSE
    assert result.runtime_preflight is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["get_pipeline_state", "request_advisor_hint", "request_interpretation_review", "unknown_tool"])
async def test_non_mutation_receipt_does_not_become_a_graph_failure(tool_name: str) -> None:
    assert not is_mutation_tool(tool_name)
    failed = replace(_failed_set_pipeline_invocation(), tool_name=tool_name)
    result = await _finalize_result("Build me a pipeline.", tool_invocations=(failed,))

    assert result.message == _MODEL_PROSE
    assert result.runtime_preflight is None


@pytest.mark.asyncio
async def test_successful_blob_only_work_does_not_become_a_failed_graph_mutation() -> None:
    assert is_mutation_tool("create_blob") and is_blob_store_only_mutation_tool("create_blob")
    receipt = _successful_receipt("create_blob", {"success": True, "data": {"blob_id": "example-blob"}})
    result = await _finalize_result("Make example CSV data.", tool_invocations=(receipt,))

    assert result.message == _MODEL_PROSE
    assert result.runtime_preflight is None


@pytest.mark.asyncio
async def test_cancellation_receipt_is_not_relabelled_as_a_recoverable_build_failure() -> None:
    receipt = replace(
        _failed_set_pipeline_invocation(),
        status=ComposerToolStatus.CANCELLED,
        error_class="CancelledError",
        error_message="coordinator_cancelled",
        error_category=None,
    )
    result = await _finalize_result("Build me a pipeline.", tool_invocations=(receipt,))

    assert result.message == _MODEL_PROSE
    assert result.runtime_preflight is None


@pytest.mark.asyncio
@pytest.mark.parametrize("trailing_tool", [None, "get_pipeline_state", "request_advisor_hint", "create_blob"])
async def test_pending_proposal_is_not_a_failed_build_even_with_non_composition_receipts(trailing_tool: str | None) -> None:
    proposal = _successful_receipt("set_pipeline", {"success": True, "data": {"status": "APPROVAL_REQUIRED"}})
    receipts = (proposal,)
    if trailing_tool is not None:
        receipts += (_successful_receipt(trailing_tool, {"success": True, "data": {}}),)
    result = await _finalize_result("Build me a pipeline.", tool_invocations=receipts)

    assert result.message == _MODEL_PROSE
    assert result.runtime_preflight is None


@pytest.mark.asyncio
async def test_later_actual_composition_failure_supersedes_a_pending_proposal() -> None:
    proposal = _successful_receipt("set_pipeline", {"success": True, "data": {"status": "APPROVAL_REQUIRED"}})
    failed = _failed_set_pipeline_invocation()
    result = await _finalize_result("Build me a pipeline.", tool_invocations=(proposal, failed))

    assert "Cause: set_pipeline failed before mutation (ToolArgumentError: source.blob_id is required)." in result.message
    assert result.runtime_preflight is not None and not result.runtime_preflight.is_valid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "detail"),
    [
        (ComposerToolStatus.ARG_ERROR, None, "set_pipeline failed before mutation"),
        (ComposerToolStatus.PLUGIN_CRASH, None, "set_pipeline crashed before a safe mutation completed"),
        (ComposerToolStatus.SUCCESS, {"success": False, "error": "source is missing"}, "set_pipeline returned success=false"),
        (ComposerToolStatus.SUCCESS, {"success": True, "data": {}}, "set_pipeline succeeded without mutating CompositionState"),
    ],
)
async def test_actual_failure_or_noop_survives_later_non_composition_receipts(
    status: ComposerToolStatus, payload: dict[str, object] | None, detail: str
) -> None:
    if status is ComposerToolStatus.SUCCESS:
        assert payload is not None
        attempt = _successful_receipt("set_pipeline", payload)
    else:
        attempt = replace(
            _failed_set_pipeline_invocation(),
            status=status,
            error_class="RuntimeError" if status is ComposerToolStatus.PLUGIN_CRASH else "ToolArgumentError",
            error_category=ToolArgumentErrorCategory.MODEL_VALIDATION if status is ComposerToolStatus.ARG_ERROR else None,
        )
    trailing = (
        replace(_failed_set_pipeline_invocation(), tool_call_id="discovery", tool_name="get_pipeline_state"),
        replace(_failed_set_pipeline_invocation(), tool_call_id="advisor", tool_name="request_advisor_hint"),
        _successful_receipt("create_blob", {"success": True, "data": {}}),
    )
    result = await _finalize_result("Explain CSV parsing without building.", tool_invocations=(attempt, *trailing))

    assert result.message.startswith(_MODEL_PROSE)
    assert detail in result.message
    assert result.raw_assistant_content == _MODEL_PROSE
    assert result.runtime_preflight is not None and not result.runtime_preflight.is_valid
    assert all(detail in check.detail for check in result.runtime_preflight.checks)
    assert all(detail in blocker.detail for blocker in result.runtime_preflight.readiness.blockers)


@pytest.mark.asyncio
async def test_mutation_success_guard_still_suppresses_failure_augmentation() -> None:
    result = await _finalize_result(
        "Build me a pipeline.", tool_invocations=(_failed_set_pipeline_invocation(),), mutation_success_seen=True
    )

    assert result.message == _MODEL_PROSE
    assert result.runtime_preflight is None
