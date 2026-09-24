"""Tool-error payload contracts shared by composer dispatch paths."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from elspeth.web.composer.audit import canonicalize_pydantic_cause, canonicalize_schema_violations
from elspeth.web.composer.protocol import SchemaViolationCode, ToolArgumentError
from elspeth.web.composer.tools._dispatch import declared_argument_names
from elspeth.web.composer.tools.wire_projection import EmptyArgumentsMarker, EnvelopeUnwrap, WireTool

INVALID_TOOL_ARGUMENTS_REDACTION_STATUS: Final[str] = "invalid_tool_arguments"
UNKNOWN_TOOL_REDACTION_STATUS: Final[str] = "unknown_tool"

if TYPE_CHECKING:
    from elspeth.web.composer.redaction_telemetry import RedactionTelemetry


def unknown_tool_arguments_redaction(*, telemetry: RedactionTelemetry) -> Mapping[str, Any]:
    """Return the fixed value-free argument shape for an unknown tool."""
    telemetry.unknown_tool_redacted()
    return {"_redaction_status": UNKNOWN_TOOL_REDACTION_STATUS}


def unknown_tool_response_redaction() -> Mapping[str, Any]:
    """Preserve the semantic failure without retaining an unredactable payload."""
    return {
        "_redaction_status": UNKNOWN_TOOL_REDACTION_STATUS,
        "success": False,
        "data": {"error": "Unknown tool"},
    }


def wire_argument_repair_message(tool: WireTool) -> str:
    """Describe a rejected wire wrapper using only the owned decode plan."""
    for node in tool.decode_plan:
        if isinstance(node, EmptyArgumentsMarker):
            marker = json.dumps({node.key: True})
            return f"Tool '{tool.name}' takes no semantic arguments. Call it with {marker} exactly."
        if isinstance(node, EnvelopeUnwrap):
            return f"Tool '{tool.name}' arguments must contain exactly one '{node.key}' object field."
    raise AssertionError(f"Tool {tool.name!r} has no rejecting wire wrapper")


def arg_error_payload(exc: ToolArgumentError, tool_name: str) -> Mapping[str, Any]:
    """Build the structured payload for an ARG_ERROR audit record and LLM tool message.

    ``validation_errors`` comes from a pydantic ``__cause__`` when there is
    one, otherwise from the S gate's closed violations (S1 T9). The compose
    loop is this function's only caller, so the S-gate repair signal reaches
    only the compose loop (MCP and planner discovery build their own bodies).
    """
    payload: dict[str, Any] = {"error": f"Tool '{tool_name}' failed: {exc.safe_message}"}
    validation_errors = canonicalize_pydantic_cause(exc.__cause__)
    if validation_errors is None:
        validation_errors = canonicalize_schema_violations(exc.schema_violations)
    if validation_errors is not None:
        payload["validation_errors"] = validation_errors
    if any(violation.code is SchemaViolationCode.UNEXPECTED and not violation.loc for violation in exc.schema_violations):
        # Only schema-owned names enter this guidance. jsonschema's message
        # contains the rejected model-authored keys and must never be echoed.
        names = declared_argument_names(tool_name)
        payload["repair_instruction"] = (
            f"Remove unsupported root properties. Allowed properties: {', '.join(names)}."
            if names
            else "This tool takes no arguments. Call it with {}."
        )
    return payload
