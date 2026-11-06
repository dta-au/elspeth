"""Detach mutable DATA from an already-admitted application graph.

This is an ownership copy, not another wire admission. It retains immutable
content and installed response contracts, copies supported mutable carriers,
and preserves reference identity across current roots and historical cuts.
Live runtime/admission owners are outside its closed nominal domain.
"""

from __future__ import annotations

from copy import copy
from types import MappingProxyType
from typing import Any, cast

from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateFailureFacts, PrivateToolAuthoring, ToolArgumentErrorCategory
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ToolContractDialect
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttempt
from elspeth.contracts.enums import AuditCharacteristic
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import FrozenJsonArray
from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability, WebConfigAuthority
from elspeth.web.composer.audit import PrivatePlannerCandidate
from elspeth.web.composer.continuation_application_graph import ApplicationGraph, ApplicationGraphCut
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, RecorderSnapshot, RetainedVisibleLink
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, _members
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tool_batch import ContinuationBatchState, ContinuationCallState
from elspeth.web.composer.turn_effects import PersistedBatchEffects, PersistedTurnEffects
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply

# These exact owned types have immutable content after source admission. Sharing
# them also preserves authoring/failure references inside shared recorder atoms.
_SHARED_TYPES = frozenset(
    (
        CompositionState,
        ValidationSummary,
        ComposerLLMCall,
        ComposerToolInvocation,
        ComposerPlannerAttempt,
        PrivatePlannerCandidate,
        PrivateToolAuthoring,
        PrivateFailureFacts,
        VisibleToolEvent,
        WithheldReply,
        RecorderDrainCursors,
        RetainedVisibleLink,
        PortableDispatchAudit,
        ToolContractDialect,
        ToolArgumentErrorCategory,
        AuditCharacteristic,
        ControlRole,
        PluginCapability,
        WebConfigAuthority,
    )
)


def _copy_members(value: Any) -> dict[str, Any]:
    """Project owned children without invoking a codec or model constructor."""
    if type(value) is ApplicationGraph:
        return {
            "selected": value.selected,
            "effects": value.effects,
            "recorder": value.recorder,
            "cuts": value.cuts,
            "selected_audit": value.selected_audit,
            "selected_dialect": value.selected_dialect,
        }
    if type(value) is ApplicationGraphCut:
        return {
            "data": value.data,
            "effects_before": value.effects_before,
            "effects_after": value.effects_after,
            "recorder_before": value.recorder_before,
            "recorder_after": value.recorder_after,
        }
    if type(value) is RestoredBatchData:
        return {"batch": value.batch, "accumulator": value.accumulator, "calls": value.calls}
    if type(value) is PersistedTurnEffects:
        return {
            "initial_state": value.initial_state,
            "initial_state_id": value.initial_state_id,
            "latest_verified_state": value.latest_verified_state,
            "latest_verified_state_id": value.latest_verified_state_id,
            "latest_committed_state_id": value.latest_committed_state_id,
            "verification": value.verification,
            "initial_authored_state_hash": value.initial_authored_state_hash,
            "latest_authored_state_hash": value.latest_authored_state_hash,
            "net_changed_fields": value.net_changed_fields,
            "batches": value.batches,
            "committed_revision_ids": value.committed_revision_ids,
        }
    if type(value) is PersistedBatchEffects:
        return {
            "assistant_message_id": value.assistant_message_id,
            "state_id": value.state_id,
            "version": value.version,
            "tool_call_ids": value.tool_call_ids,
            "inserted_state_ids": value.inserted_state_ids,
            "authored_state_hash": value.authored_state_hash,
        }
    if type(value) is RecorderSnapshot:
        return {
            "invocations": value.invocations,
            "llm_calls": value.llm_calls,
            "planner_attempts": value.planner_attempts,
            "withheld_replies": value.withheld_replies,
            "visible_tool_events": value.visible_tool_events,
            "private_candidates": value.private_candidates,
            "private_authorings": value.private_authorings,
            "drained": value.drained,
        }
    if type(value) is AdmittedDiscoveryResult:
        # The batch wire inventory substitutes tool_name for contract. A copy
        # instead retains the installed contract (including parser callbacks)
        # by identity on the shallow-copied carrier.
        return {"result": value.result, "response": value.response}
    if type(value) is ContinuationBatchState and (
        value.service_failure is not None
        or value.manual_advisor_allowance is not None
        or value.plugin_crash is not None
        or value.plugin_crash_cause is not None
        or value.advisor_failure is not None
        or value.p4 is not None
    ):
        raise AuditIntegrityError("application DATA copy cannot retain live batch owners")
    if type(value) is ContinuationCallState and value.audit is not None:
        raise AuditIntegrityError("application DATA copy cannot retain a live dispatch audit")
    # This existing exact-type inventory refuses unsupported nominal objects.
    # AdmittedPluginSchemaResponse projects its snapshot; the discovery
    # contract is handled above.
    return _members(value)[1]


def copy_application_graph(graph: ApplicationGraph) -> ApplicationGraph:
    """Copy admitted graph DATA for a separate consumer, preserving all aliases.

    The caller owns source admission and must keep the source quiescent while
    copying. No credentials, hashes, JSON schemas or recorder history are
    revalidated here. No live owner or arbitrary plugin object is copied.
    """
    if type(graph) is not ApplicationGraph:
        raise AuditIntegrityError("application DATA copy requires its nominal graph")
    memo: dict[int, Any] = {}

    def visit(value: Any) -> Any:
        result: Any
        value_type = type(value)
        if value is None or value_type in (str, int, float, bool):
            return value
        identity = id(value)
        if identity in memo:
            return memo[identity]
        if value_type in _SHARED_TYPES:
            memo[identity] = value
            return value
        if value_type is list:
            items: list[Any] = []
            memo[identity] = items
            items.extend(visit(item) for item in value)
            return items
        if value_type in (dict, MappingProxyType):
            members: dict[str, Any] = {}
            result = MappingProxyType(members) if value_type is MappingProxyType else members
            memo[identity] = result
            for key, item in value.items():
                members[key] = visit(item)
            return result
        if value_type is set:
            elements: set[Any] = set()
            memo[identity] = elements
            elements.update(visit(item) for item in value)
            return elements
        if value_type in (tuple, FrozenJsonArray, frozenset):
            # Source admission has already refused cycles. Preserve the exact
            # container kind and never merge equal but distinct source nodes.
            children = [visit(item) for item in value]
            if value_type is tuple:
                result = tuple(children)
            elif value_type is FrozenJsonArray:
                result = FrozenJsonArray(children)
            else:
                result = frozenset(children)
            memo[identity] = result
            return result
        fields = _copy_members(value)
        result = copy(value)
        memo[identity] = result
        for name, item in fields.items():
            # A shallow copy plus explicit owned fields avoids constructors
            # refreezing mappings or Pydantic rebuilding shared child lists.
            object.__setattr__(result, name, visit(item))
        return result

    return cast(ApplicationGraph, visit(graph))
