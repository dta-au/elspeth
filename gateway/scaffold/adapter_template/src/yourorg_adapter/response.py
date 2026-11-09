"""TEMPLATE: translate an upstream success body into a CanonicalResponse.

Read ``elspeth_llm_gateway.reference.adapter.ReferenceV1InvokeAdapter.parse_success``
alongside this file for the worked example.
"""

from elspeth_llm_gateway.sdk.types import CanonicalResponse


def parse_success(body: dict) -> CanonicalResponse:
    # TRANSLATION POINT: build a CanonicalResponse from your upstream's real
    # success body.
    #
    # CanonicalResponse enforces exactly one of `text` (a string, possibly
    # "") or `tool_calls` (non-empty) -- never both, never neither -- and
    # raises ValueError otherwise. Pick the right FinishReason
    # (STOP / LENGTH / TOOL_CALLS / CONTENT_FILTER) for each of your
    # upstream's completion-status values.
    #
    # If you declare Capability.REASONING, return the model's thinking for
    # this turn as CanonicalResponse.thinking_blocks (CanonicalThinkingBlock,
    # in order, opaque signature/data unmodified). The caller replays them on
    # the next tool-loop turn.
    raise NotImplementedError("yourorg_adapter.response.parse_success: implement against your agency's real success body")
