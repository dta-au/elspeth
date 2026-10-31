"""The model-facing Continuation control: ``checkpoint_and_continue`` (U-40).

A provider-authored request that the current segment yield at its next
handoff-safe cut, so the same request continues in a new segment. It is a
control, not graph authoring: it carries optional notes only, it must be the
sole call in its batch, and the host never derives the next action from it.
Notes are model text; they never certify progress (design §2.1, I-4).

Only lifecycle-2 (Continuation) compose palettes advertise it; a v1 job has
no segment to yield.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.tool_calls import PROVIDER_TOOL_CALL_ID_MAX_LENGTH
from elspeth.web.credential_guard import require_no_credential_material

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _AdmittedToolBatch

CONTINUATION_CONTROL_TOOL: Final = "checkpoint_and_continue"
CONTINUATION_NOTES_MAX_BYTES: Final = 500
CONTINUATION_CALL_NOT_ALONE: Final = "CONTINUATION_CALL_NOT_ALONE"
CONTINUATION_UNAVAILABLE: Final = "CONTINUATION_UNAVAILABLE"

# The tool result never claims a successor is queued or ran: the yield commits
# after the tool returns, at the next handoff-safe cut.
CONTINUATION_CONTROL_RESULT: Final = (
    "Checkpoint requested. The current step of this request ends at its next safe point and the same request "
    "continues in a new step. Nothing has run yet in that step."
)


class ContinueArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    notes: str | None = Field(
        default=None,
        description="Optional short notes for the next step (at most 500 UTF-8 bytes). Notes never count as progress.",
    )


class ContinuationControlIntent(BaseModel):
    """The admitted request, bound to its provider tool call."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tool_call_id: str = Field(min_length=1, max_length=PROVIDER_TOOL_CALL_ID_MAX_LENGTH)
    notes: str | None


class ContinuationControlRefused(ValueError):
    """A refused control call; the batch is refused before any mutation or discovery effect."""

    def __init__(self, error_code: Literal["CONTINUATION_CALL_NOT_ALONE", "CONTINUATION_UNAVAILABLE"], detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail

    def to_tool_payload(self) -> dict[str, str]:
        # Literal codes, so the closed-registry census sees each producer.
        if self.error_code == "CONTINUATION_CALL_NOT_ALONE":
            return {"status": "ARG_ERROR", "error": self.detail, "error_code": "CONTINUATION_CALL_NOT_ALONE"}
        return {"status": "ARG_ERROR", "error": self.detail, "error_code": "CONTINUATION_UNAVAILABLE"}


def continuation_control_tool_definitions(dialect: ToolContractDialect) -> list[dict[str, object]]:
    """Fresh function schema; every property is required for strict transports."""
    schema = ContinueArguments.model_json_schema()
    if dialect is ToolContractDialect.OPENAI_STRICT:
        # Strict transports require every property listed; null stands for "no notes".
        schema["required"] = ["notes"]
    function: dict[str, object] = {
        "name": CONTINUATION_CONTROL_TOOL,
        "description": (
            "Ask to continue this same request in a new step: the current step ends at its next safe point and the "
            "request carries on. Use alone, never in a batch. Use only when there is more productive work and this "
            "step is running out of room. It does not finish the request, ask the user anything, or change the pipeline."
        ),
        "parameters": schema,
    }
    if dialect is ToolContractDialect.OPENAI_STRICT:
        function["strict"] = True
    return [{"type": "function", "function": function}]


def parse_continuation_control_batch(batch: _AdmittedToolBatch) -> ContinuationControlIntent | None:
    """None when the batch has no control call; refuse a mixed batch before any effect."""
    if not any(call.function.name == CONTINUATION_CONTROL_TOOL for call in batch.calls):
        return None
    if len(batch.calls) != 1:
        raise ContinuationControlRefused(
            CONTINUATION_CALL_NOT_ALONE,
            "checkpoint_and_continue must be the only tool call in its batch; every call in this batch was refused before effects.",
        )
    call = batch.calls[0]
    arguments = json.loads(call.function.arguments)
    if type(arguments) is not dict:
        raise ValueError("checkpoint_and_continue arguments must be an object")
    parsed = ContinueArguments.model_validate(arguments)
    notes = parsed.notes
    if notes is not None:
        if not notes.strip() or len(notes.encode("utf-8")) > CONTINUATION_NOTES_MAX_BYTES:
            raise ValueError("checkpoint_and_continue notes must be 1 to 500 UTF-8 bytes")
        require_no_credential_material(notes, surface="composer_continuation_notes")
    return ContinuationControlIntent(tool_call_id=call.id, notes=notes)
