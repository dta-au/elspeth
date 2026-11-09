"""Provider-authored terminal recovery controls, separate from graph mutation dispatch."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.graph_recovery import GraphRecoveryContext, GraphRecoveryIntent
from elspeth.web.credential_guard import require_no_credential_material

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _AdmittedToolBatch

RECOVERY_TOOL_NAMES = frozenset({"handoff_to_user", "restore_parked_graph"})


class HandoffArguments(BaseModel):
    """A terminal request, never an assertion that recovery already succeeded."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["park", "reset_to_turn_start", "reset_to_safe_checkpoint"]
    message: str = Field(min_length=1, max_length=16000)


class RestoreArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    park_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=16000)


def recovery_tool_definitions(dialect: ToolContractDialect) -> list[dict[str, object]]:
    """Fresh function schemas; every property is required for strict transports."""
    result: list[dict[str, object]] = []
    for name, model, description in (
        (
            "handoff_to_user",
            HandoffArguments,
            "End authoring immediately and talk to the user. Park keeps the visible draft; reset_to_turn_start restores "
            "the exact starting snapshot; reset_to_safe_checkpoint restores an editable checkpoint or an empty authoring "
            "state if none exists. Reset does not require saving the broken draft. Use alone, never in a batch. "
            "The server reports the actual outcome; do not claim success before its receipt.",
        ),
        (
            "restore_parked_graph",
            RestoreArguments,
            "Restore the exact advertised parked graph and end this turn with your message. Use alone. "
            "Restoration grants no execution readiness and does not undo external effects.",
        ),
    ):
        schema = model.model_json_schema()
        # Length limits are enforced at admission. The strict grammar subset
        # does not consistently support these keywords across transports.
        for property_schema in schema["properties"].values():
            if "minLength" in property_schema:
                del property_schema["minLength"]
            if "maxLength" in property_schema:
                del property_schema["maxLength"]
        function: dict[str, object] = {"name": name, "description": description, "parameters": schema}
        if dialect is ToolContractDialect.OPENAI_STRICT:
            function["strict"] = True
        result.append({"type": "function", "function": function})
    return result


def parse_recovery_tool_call(name: str, arguments: Mapping[str, Any], tool_call_id: str) -> GraphRecoveryIntent | None:
    """Validate external arguments before constructing the owned request."""
    if name == "handoff_to_user":
        handoff = HandoffArguments.model_validate(dict(arguments))
        if not handoff.message.strip():
            raise ValueError("Recovery message must not be blank")
        require_no_credential_material(handoff.message, surface="composer_recovery_reply")
        return GraphRecoveryIntent(handoff.action, handoff.message, None, tool_call_id)
    if name == "restore_parked_graph":
        restore = RestoreArguments.model_validate(dict(arguments))
        if not restore.message.strip() or not restore.park_id.strip():
            raise ValueError("Recovery message and parked identity must not be blank")
        require_no_credential_material(restore.message, surface="composer_recovery_reply")
        return GraphRecoveryIntent("restore_parked_graph", restore.message, restore.park_id, tool_call_id)
    return None


def parse_recovery_tool_batch(batch: _AdmittedToolBatch) -> GraphRecoveryIntent | None:
    """Reject mixed control batches before any mutation or discovery effect."""
    if not any(call.function.name in RECOVERY_TOOL_NAMES for call in batch.calls):
        return None
    if len(batch.calls) != 1:
        raise ValueError("Recovery controls must be the sole tool call in a response")
    call = batch.calls[0]
    arguments = json.loads(call.function.arguments)
    if type(arguments) is not dict:
        raise ValueError("Recovery arguments must be an object")
    return parse_recovery_tool_call(call.function.name, arguments, call.id)


def recovery_context_message(context: GraphRecoveryContext) -> str:
    """Only scalar, server-owned identities enter the provider recovery context."""
    return json.dumps(
        {
            "turn_start_state_id": context.turn_start_state_id,
            "turn_start_version": context.turn_start_version,
            "current_state_id": context.current_state_id,
            "current_version": context.current_version,
            "malformed_head": context.malformed_head,
            "parked_graphs": [
                {"park_id": park.park_id, "state_id": park.state_id, "version": park.version} for park in context.parked_graphs
            ],
        },
        sort_keys=True,
    )
