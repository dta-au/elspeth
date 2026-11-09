"""Explicit provider-authored outcomes for freeform no-tool replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.web.composer.bounded_json import JsonBoundaryError, require_bounded_text
from elspeth.web.composer.no_tool_policy import state_is_structurally_empty
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import is_discovery_tool


@dataclass(frozen=True, slots=True)
class NoToolResponse:
    """Parsed protocol intent and model-authored body; neither grants readiness."""

    kind: Literal["reply", "clarification"]
    body: str


def parse_no_tool_response(content: str) -> NoToolResponse:
    """Admit only an exact leading marker with a nonempty bounded body.

    Ordinary prose, malformed markers and planner ``DECLINE:`` retain their
    original meaning and bytes. No question or natural-language classifier
    selects clarification behavior.
    """
    marker = "CLARIFY:"
    if content.startswith(marker):
        try:
            text = require_bounded_text(content, label="no-tool assistant content")
        except JsonBoundaryError:
            return NoToolResponse(kind="reply", body=content)
        body = text[len(marker) :].lstrip()
        if body.strip():
            return NoToolResponse(kind="clarification", body=body)
    return NoToolResponse(kind="reply", body=content)


def is_unstarted_clarification(
    response: NoToolResponse,
    *,
    state: CompositionState,
    initial_version: int,
    mutation_success_seen: bool,
    tool_invocations: tuple[ComposerToolInvocation, ...],
) -> bool:
    """Exempt only unchanged empty drafts with zero or discovery-only receipts.

    Any non-discovery receipt preserves the existing failure/proposal policy,
    regardless of how the model labels its reply.
    """
    return (
        response.kind == "clarification"
        and state.version == initial_version
        and state_is_structurally_empty(state)
        and not mutation_success_seen
        and all(is_discovery_tool(invocation.tool_name) for invocation in tool_invocations)
    )
