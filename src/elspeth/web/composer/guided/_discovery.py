"""Shared discovery-loop primitives for guided per-phase chat solvers.

The source and sink chat solvers expose read-only discovery tools to the
composer model so it can inspect real plugin schemas before resolving a stage.

These functions carry NO solver-specific policy: the *caller* owns the
execution-side safety gate (proving a call is an allowed discovery tool before
dispatching), the terminal handling, and the iteration cap. ``_execute_discovery_call``
trusts that its ``tool_call`` was already gated.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from elspeth.contracts.secrets import WebSecretResolver
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import (
    BufferingRecorder,
    begin_dispatch_or_arg_error,
    finish_arg_error,
    finish_plugin_crash,
    finish_success,
)
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.guided.errors import GuidedSolverResponseShapeError
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


def _record_shape_arg_error(
    *,
    recorder: BufferingRecorder,
    tool_call_id: str,
    tool_name: str,
    audit_arguments: Mapping[str, Any],
    version_before: int,
    actor: str,
    error_class: str,
) -> None:
    """Record a value-free ARG_ERROR for malformed provider argument shape."""
    audit, canonicalization_failed = begin_dispatch_or_arg_error(
        tool_call_id,
        tool_name,
        audit_arguments,
        version_before=version_before,
        actor=actor,
    )
    if canonicalization_failed is not None:
        raise RuntimeError("guided shape-error audit sentinel was not canonicalizable") from canonicalization_failed
    recorder.record(
        finish_arg_error(
            audit,
            error_class=error_class,
            error_message=error_class,
        )
    )


def _assistant_tool_calls_message(message: Any, tool_calls: Any) -> dict[str, Any]:
    """Re-materialise the assistant turn that requested *tool_calls*.

    The OpenAI/LiteLLM tool protocol requires the assistant message carrying
    the tool-call request to precede the ``role=tool`` results in the next
    round, and every ``tool_call_id`` it names must be answered. We rebuild it
    explicitly (rather than re-appending the raw provider object) so the wire
    shape is deterministic and provider-agnostic.
    """
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
            }
            for tool_call in tool_calls
        ],
    }


def _execute_discovery_call(
    *,
    tool_call: Any,
    state: CompositionState,
    catalog: PolicyCatalogView,
    plugin_snapshot: PluginAvailabilitySnapshot,
    secret_service: WebSecretResolver | None,
    user_id: str | None,
    actor: str,
    recorder: BufferingRecorder | None,
) -> dict[str, Any]:
    """Dispatch one read-only discovery call, audit it, and build its result message.

    The caller has already proven ``tool_call.function.name`` is an allowed
    discovery tool (the execution-side safety gate), so dispatching through
    ``execute_tool`` — whose handler union also contains mutation tools — is
    safe here. Discovery tools never advance ``CompositionState`` so
    ``version_before == version_after``.

    A semantically-failed result (e.g. ``get_plugin_schema`` for an unknown
    plugin) is still threaded back to the model verbatim so it can correct
    itself; that matches the freeform loop's behaviour.
    """
    function = tool_call.function
    name = function.name
    raw_arguments = function.arguments
    if raw_arguments is not None and not isinstance(raw_arguments, str):
        if recorder is not None:
            _record_shape_arg_error(
                recorder=recorder,
                tool_call_id=tool_call.id,
                tool_name=name,
                audit_arguments={
                    "_argument_shape_error": "non_string",
                    "_actual_type": type(raw_arguments).__name__,
                },
                version_before=state.version,
                actor=actor,
                error_class="GuidedSolverResponseShapeError",
            )
        raise GuidedSolverResponseShapeError(f"{name} arguments must be a JSON string; got {type(raw_arguments).__name__}")
    # Malformed tool-call arguments are an LLM RESPONSE-SHAPE failure, not a
    # server/client bug: route them through the typed model-response failure so
    # step chat can emit its closed synthetic-unavailable response.
    try:
        parsed = json.loads(raw_arguments) if isinstance(raw_arguments, str) and raw_arguments.strip() else {}
    except json.JSONDecodeError as exc:
        if recorder is not None:
            _record_shape_arg_error(
                recorder=recorder,
                tool_call_id=tool_call.id,
                tool_name=name,
                audit_arguments={
                    "_argument_shape_error": "invalid_json",
                    "_raw_length": len(raw_arguments or ""),
                },
                version_before=state.version,
                actor=actor,
                error_class=type(exc).__name__,
            )
        raise GuidedSolverResponseShapeError(f"{name} arguments are not valid JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        if recorder is not None:
            _record_shape_arg_error(
                recorder=recorder,
                tool_call_id=tool_call.id,
                tool_name=name,
                audit_arguments={
                    "_argument_shape_error": "non_object",
                    "_actual_type": type(parsed).__name__,
                },
                version_before=state.version,
                actor=actor,
                error_class="GuidedSolverResponseShapeError",
            )
        raise GuidedSolverResponseShapeError(f"{name} arguments must decode to an object; got {type(parsed).__name__}")
    arguments = dict(parsed)
    if recorder is None:
        try:
            result = execute_tool(
                name,
                arguments,
                state,
                catalog,
                plugin_snapshot=plugin_snapshot,
                secret_service=secret_service,
                user_id=user_id,
                validate_arguments=True,
                require_data_dir_for_paths=True,
                raise_schema_argument_errors=True,
            )
        except ToolArgumentError as exc:
            raise GuidedSolverResponseShapeError(str(exc)) from exc
        return {"role": "tool", "tool_call_id": tool_call.id, "content": serialize_tool_result(result)}

    audit, canonicalization_failed = begin_dispatch_or_arg_error(
        tool_call.id,
        name,
        arguments,
        version_before=state.version,
        actor=actor,
    )
    invocation = None
    try:
        if canonicalization_failed is not None:
            invocation = finish_arg_error(
                audit,
                error_class=type(canonicalization_failed).__name__,
                error_message=type(canonicalization_failed).__name__,
            )
            raise GuidedSolverResponseShapeError(f"{name} arguments could not be canonicalized") from canonicalization_failed
        result = execute_tool(
            name,
            arguments,
            state,
            catalog,
            plugin_snapshot=plugin_snapshot,
            secret_service=secret_service,
            user_id=user_id,
            validate_arguments=True,
            require_data_dir_for_paths=True,
            raise_schema_argument_errors=True,
        )
        message = {"role": "tool", "tool_call_id": tool_call.id, "content": serialize_tool_result(result)}
        invocation = finish_success(audit, result_payload=result.to_dict(), version_after=state.version)
        return message
    except ToolArgumentError as exc:
        invocation = finish_arg_error(
            audit,
            error_class=type(exc).__name__,
            error_message=str(exc),
        )
        raise GuidedSolverResponseShapeError(str(exc)) from exc
    except GuidedSolverResponseShapeError:
        raise
    except BaseException as exc:
        invocation = finish_plugin_crash(audit, exc=exc)
        raise
    finally:
        if invocation is None:
            current_exc = RuntimeError("guided discovery dispatch reached finalization without a disposition")
            recorder.record(finish_plugin_crash(audit, exc=current_exc))
            raise current_exc
        recorder.record(invocation)
