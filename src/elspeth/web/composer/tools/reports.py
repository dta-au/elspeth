"""``report_composer_bug``: a bounded, session-owned record of a suspected guidance/tool mismatch.

The model calls this when the composer skill or a tool description promises
one behaviour and a tool call showed another. The handler writes one JSON
report file into the session's files, with the composer context that
produced it, and changes nothing else: the pipeline state is returned
unchanged, nothing is repaired and nothing is verified. The report says so
in its own ``status`` field, because a reader of the file later cannot see
the conversation that produced it.

The report is an ordinary assistant-created session blob. It goes through
the same preparation, provenance and quota custody as ``create_blob``
(:func:`_prepare_blob_create`, :func:`_blob_creation_provenance`,
:func:`_persist_prepared_blob_create`); this module only builds the content.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, TypedDict
from uuid import uuid4

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.composer.protocol import (
    COMPOSER_BUG_REPORT_MAX_BYTES,
    COMPOSER_BUG_REPORT_TOO_LARGE_ACTUAL_TYPE,
    COMPOSER_BUG_REPORT_TOO_LARGE_EXPECTATION,
    ToolArgumentError,
)
from elspeth.web.composer.redaction import (
    REPORT_BEHAVIOR_MAX_CHARS,
    REPORT_GUIDANCE_REFERENCE_MAX_CHARS,
    REPORT_RELATED_TOOL_MAX_CHARS,
    REPORT_RELATED_TOOL_PATTERN,
    REPORT_SUMMARY_MAX_CHARS,
    ReportComposerBugArgumentsModel,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import (
    ToolContext,
    ToolResult,
    _discovery_result,
    _failure_result,
    _validate_mutation_arguments,
)
from elspeth.web.composer.tools.blobs import (
    _blob_creation_provenance,
    _persist_prepared_blob_create,
    _prepare_blob_create,
    _PreparedBlobCreate,
)
from elspeth.web.composer.tools.declarations import ToolDeclaration, ToolKind

__all__ = ["REPORT_STATUS", "REPORT_TYPE", "TOOLS_IN_MODULE", "ComposerBugReportPayload"]

# The saved file is bounded by ``COMPOSER_BUG_REPORT_MAX_BYTES`` (UTF-8 bytes)
# as well as by the field caps (characters). The caps alone do not bound it:
# the report is written as UTF-8 JSON, so a character takes one to four bytes,
# and JSON escapes a quote, a backslash or a control character. Ordinary ASCII
# prose at every field cap fits (10,136 bytes measured), and so does one
# behaviour field at its cap in two- or three-byte text (four-byte text does
# not); a larger report is refused before anything is written. The tool
# description states the byte limit, so the model is not promised more than
# the file can hold.

REPORT_TYPE: Final = "suspected_composer_bug"
REPORT_STATUS: Final = "suspected_unverified"
_REPORT_MIME_TYPE: Final = "application/json"
_REPORT_DESCRIPTION: Final = "Suspected Composer guidance or tool mismatch, reported by the composer model; not verified."


class ComposerBugReportPayload(TypedDict):
    """The tool result: which session file holds the report. The report prose is not echoed."""

    blob_id: str
    filename: str
    status: str


def _report_payload(prepared: _PreparedBlobCreate) -> ComposerBugReportPayload:
    """Name the saved report file; the prose stays in the file."""
    return ComposerBugReportPayload(blob_id=prepared.blob_id, filename=prepared.filename, status=REPORT_STATUS)


def _report_observations(report: ReportComposerBugArgumentsModel) -> dict[str, str]:
    """The supplied observations, field by field; an omitted optional field stays absent."""
    observations = {
        "summary": report.summary,
        "expected_behavior": report.expected_behavior,
        "observed_behavior": report.observed_behavior,
    }
    if report.guidance_reference is not None:
        observations["guidance_reference"] = report.guidance_reference
    if report.related_tool is not None:
        observations["related_tool"] = report.related_tool
    return observations


def _report_content(observations: Mapping[str, str], state: CompositionState, context: ToolContext) -> str:
    """Render the report file: the observations as supplied, plus server-owned context."""
    document = {
        "report_type": REPORT_TYPE,
        "status": REPORT_STATUS,
        # Fixed width: the file size must not depend on whether the clock read a whole second.
        "reported_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "observations": dict(observations),
        "context": {
            "session_id": context.session_id,
            "state_id": context.current_state_id,
            "state_version": state.version,
            "user_message_id": context.user_message_id,
            "model": context.composer_model_identifier,
            "model_version": context.composer_model_version,
            "provider": context.composer_provider,
            "skill_hash": context.composer_skill_hash,
            "tool_arguments_hash": context.tool_arguments_hash,
        },
    }
    return json.dumps(document, ensure_ascii=False, indent=2)


@trust_boundary(
    tier=3,
    source="LLM composer tool-call arguments (report_composer_bug)",
    source_param="arguments",
    suppresses=(),
    invariant=(
        "raises ToolArgumentError when the arguments do not conform to ReportComposerBugArgumentsModel or the "
        "rendered report exceeds COMPOSER_BUG_REPORT_MAX_BYTES; writes nothing in either case and never truncates or coerces"
    ),
    test_ref="tests/unit/web/composer/test_report_composer_bug.py::test_handler_refuses_malformed_or_oversize_arguments_without_writing",
    test_fingerprint="01a93774a9f83313b9c8d02eaeff0c3f3df5c629fcd1f050e73d55cbb3240369",
)
def _execute_report_composer_bug(
    arguments: dict[str, object],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Save one suspected-mismatch report as a session file; never touch the pipeline."""
    report = _validate_mutation_arguments(ReportComposerBugArgumentsModel, arguments, "report_composer_bug arguments")
    content = _report_content(_report_observations(report), state, context)
    if len(content.encode("utf-8")) > COMPOSER_BUG_REPORT_MAX_BYTES:
        raise ToolArgumentError(
            argument="report_composer_bug arguments",
            expected=COMPOSER_BUG_REPORT_TOO_LARGE_EXPECTATION,
            actual_type=COMPOSER_BUG_REPORT_TOO_LARGE_ACTUAL_TYPE,
            category=ToolArgumentErrorCategory.SEMANTIC_RULE,
        )

    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    if context.data_dir is None:
        return _failure_result(state, "Blob tools require data_dir for storage.")

    provenance = _blob_creation_provenance(content, context)
    prepared = _prepare_blob_create(
        filename=f"composer-bug-report-{uuid4()}.json",
        mime_type=_REPORT_MIME_TYPE,
        content=content,
        description=_REPORT_DESCRIPTION,
        data_dir=context.data_dir,
        session_id=session_id,
        creation_modality=provenance.creation_modality,
        created_from_message_id=context.user_message_id,
        creating_model_identifier=provenance.creating_model_identifier,
        creating_model_version=provenance.creating_model_version,
        creating_provider=provenance.creating_provider,
        creating_composer_skill_hash=provenance.creating_composer_skill_hash,
        creating_arguments_hash=provenance.creating_arguments_hash,
    )
    quota_error = _persist_prepared_blob_create(
        prepared,
        session_engine=session_engine,
        session_id=session_id,
        max_blob_storage_per_session_bytes=context.max_blob_storage_per_session_bytes,
        session_operation_context=context.session_operation_context,
        session_operation_authority=context.session_operation_authority,
    )
    if quota_error is not None:
        return _failure_result(state, quota_error)
    return _discovery_result(state, _report_payload(prepared))


_REPORT_COMPOSER_BUG_DECLARATION: Final = ToolDeclaration(
    name="report_composer_bug",
    handler=_execute_report_composer_bug,
    kind=ToolKind.BLOB_MUTATION,
    blob_store_only=True,
    description=(
        "Record a suspected Composer bug: the skill or a tool description says one thing and a tool call did another. "
        "Saves a JSON report file in this session and returns its `blob_id`, `filename` and `status` "
        "(always `suspected_unverified`). "
        "Reporting does not change the pipeline, repair the problem or confirm the bug. "
        "Report each distinct mismatch once, then continue the user's task. "
        f"The saved report is at most {COMPOSER_BUG_REPORT_MAX_BYTES:,} bytes of UTF-8 JSON. "
        "State factual observations only; never include credentials or document content the mismatch does not need."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": REPORT_SUMMARY_MAX_CHARS,
                "description": "One-line summary of the suspected mismatch.",
            },
            "expected_behavior": {
                "type": "string",
                "minLength": 1,
                "maxLength": REPORT_BEHAVIOR_MAX_CHARS,
                "description": "What the skill or tool description says should happen.",
            },
            "observed_behavior": {
                "type": "string",
                "minLength": 1,
                "maxLength": REPORT_BEHAVIOR_MAX_CHARS,
                "description": "What the tool call actually did, including the relevant error text.",
            },
            "guidance_reference": {
                "type": "string",
                "minLength": 1,
                "maxLength": REPORT_GUIDANCE_REFERENCE_MAX_CHARS,
                "description": "The skill section or tool description being contradicted.",
            },
            "related_tool": {
                "type": "string",
                "maxLength": REPORT_RELATED_TOOL_MAX_CHARS,
                "pattern": REPORT_RELATED_TOOL_PATTERN,
                "description": "Name of the composer tool involved, if any.",
            },
        },
        "required": ["summary", "expected_behavior", "observed_behavior"],
        "additionalProperties": False,
    },
)

TOOLS_IN_MODULE: Final[tuple[ToolDeclaration, ...]] = (_REPORT_COMPOSER_BUG_DECLARATION,)
