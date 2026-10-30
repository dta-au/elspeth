"""Actual effects/recorder/batch graph roots for portable P4 application DATA.

The existing coupled encoder owns node identities. This module supplies only
explicit additional nominal members and constructors; no reflective discovery.
Existing admitted state/validation and frozen audit atoms keep their owned value
contracts. Private authoring identity is explicit across recorder and batch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pydantic import TypeAdapter

from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateFailureFacts
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ToolContractDialect
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttempt
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import PrivatePlannerCandidate
from elspeth.web.composer.continuation_audit_buffer import (
    RecorderDrainCursors,
    RecorderSnapshot,
    RetainedVisibleLink,
    encode_recorder_snapshot,
)
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import PersistedBatchEffects, PersistedTurnEffects, authored_state_hash
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.credential_guard import require_no_credential_material


@dataclass(frozen=True, slots=True)
class ApplicationGraphCut:
    data: RestoredBatchData
    effects_before: PersistedTurnEffects
    effects_after: PersistedTurnEffects
    recorder_before: RecorderSnapshot
    recorder_after: RecorderSnapshot


@dataclass(frozen=True, slots=True)
class ApplicationGraph:
    selected: RestoredBatchData
    effects: PersistedTurnEffects
    recorder: RecorderSnapshot
    cuts: tuple[ApplicationGraphCut, ...]
    selected_audit: PortableDispatchAudit | None = None
    selected_dialect: ToolContractDialect | None = None


_ATOMS: dict[str, type[Any]] = {
    "application_llm": ComposerLLMCall,
    "application_planner": ComposerPlannerAttempt,
    "application_candidate": PrivatePlannerCandidate,
    "application_withheld": WithheldReply,
    "application_visible": VisibleToolEvent,
    "application_failure": PrivateFailureFacts,
    "application_cursors": RecorderDrainCursors,
}


def _atom_json(value: Any, owner: type[Any]) -> str:
    if type(value) is VisibleToolEvent and VisibleToolEvent.from_envelope(value.to_envelope()) != value:
        raise AuditIntegrityError("application visible event changed its actual historical envelope")
    adapter = TypeAdapter(owner)
    encoded = canonical_json(adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json))
    checked = adapter.validate_json(encoded, strict=True, extra="forbid")
    if canonical_json(adapter.dump_python(checked, mode="json", warnings="error", fallback=owned_json)) != encoded:
        raise AuditIntegrityError("application audit atom changed its complete owned fields")
    return encoded


def application_members(value: Any) -> tuple[str, dict[str, Any]] | None:
    if type(value) is ApplicationGraphCut:
        return "application_cut", {
            "data": value.data,
            "effects_before": value.effects_before,
            "effects_after": value.effects_after,
            "recorder_before": value.recorder_before,
            "recorder_after": value.recorder_after,
        }
    if type(value) is RestoredBatchData:
        return "application_batch", {"batch": value.batch, "accumulator": value.accumulator, "calls": value.calls}
    if type(value) is PersistedTurnEffects:
        return "application_effects", {
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
        return "application_batch_effects", {
            "assistant_message_id": value.assistant_message_id,
            "state_id": value.state_id,
            "version": value.version,
            "tool_call_ids": value.tool_call_ids,
            "inserted_state_ids": value.inserted_state_ids,
            "authored_state_hash": value.authored_state_hash,
        }
    if type(value) is RecorderSnapshot:
        encode_recorder_snapshot(value)
        return "application_recorder", {
            "invocations": value.invocations,
            "llm_calls": value.llm_calls,
            "planner_attempts": value.planner_attempts,
            "withheld_replies": value.withheld_replies,
            "visible_tool_events": value.visible_tool_events,
            "private_candidates": value.private_candidates,
            "private_authorings": value.private_authorings,
            "drained": value.drained,
        }
    if type(value) is RetainedVisibleLink:
        return "application_visible_link", {"invocation_index": value.invocation_index, "event": value.event}
    if type(value) is ComposerToolInvocation:
        # The scalar/timestamp/status part retains its exact existing nominal
        # codec; authoring/failure links are separate nodes, never equal-value
        # interned. Constructor admission and the full inner round trip occur.
        return "application_invocation", {
            "canonical": _atom_json(value, ComposerToolInvocation),
            "private_authoring": value.private_authoring,
            "private_failure": value.private_failure,
        }
    for kind, owner in _ATOMS.items():
        if type(value) is owner:
            return kind, {"canonical": _atom_json(value, owner)}
    return None


_FIELDS = {
    "application_cut": ("data", "effects_before", "effects_after", "recorder_before", "recorder_after"),
    "application_batch": ("batch", "accumulator", "calls"),
    "application_effects": (
        "initial_state",
        "initial_state_id",
        "latest_verified_state",
        "latest_verified_state_id",
        "latest_committed_state_id",
        "verification",
        "initial_authored_state_hash",
        "latest_authored_state_hash",
        "net_changed_fields",
        "batches",
        "committed_revision_ids",
    ),
    "application_batch_effects": (
        "assistant_message_id",
        "state_id",
        "version",
        "tool_call_ids",
        "inserted_state_ids",
        "authored_state_hash",
    ),
    "application_recorder": (
        "invocations",
        "llm_calls",
        "planner_attempts",
        "withheld_replies",
        "visible_tool_events",
        "private_candidates",
        "private_authorings",
        "drained",
    ),
    "application_visible_link": ("invocation_index", "event"),
    "application_invocation": ("canonical", "private_authoring", "private_failure"),
}


def construct_application_member(kind: str, members: dict[str, Any]) -> Any:
    expected = ("canonical",) if kind in _ATOMS else _FIELDS.get(kind)
    if expected is None or tuple(members) != expected:
        raise AuditIntegrityError("application graph changed its exact nominal field set/order")
    if kind in _ATOMS:
        owner = _ATOMS[kind]
        restored = TypeAdapter(owner).validate_json(members["canonical"], strict=True, extra="forbid")
        if _atom_json(restored, owner) != members["canonical"]:
            raise AuditIntegrityError("application atom changed its retained inner bytes")
        return restored
    if kind == "application_invocation":
        invocation = TypeAdapter(ComposerToolInvocation).validate_json(members["canonical"], strict=True, extra="forbid")
        if invocation.private_authoring != members["private_authoring"] or invocation.private_failure != members["private_failure"]:
            raise AuditIntegrityError("application invocation changed its private source links")
        linked = replace(invocation, private_authoring=members["private_authoring"], private_failure=members["private_failure"])
        if _atom_json(linked, ComposerToolInvocation) != members["canonical"]:
            raise AuditIntegrityError("application invocation changed its complete original bytes")
        return linked
    if kind == "application_cut":
        return ApplicationGraphCut(**members)
    if kind == "application_batch":
        return RestoredBatchData(**members)
    if kind == "application_effects":
        return PersistedTurnEffects(**members)
    if kind == "application_batch_effects":
        return PersistedBatchEffects(**members)
    if kind == "application_recorder":
        snapshot = RecorderSnapshot(**members)
        snapshot.validate_links_and_cursors()
        return snapshot
    if kind == "application_visible_link":
        return RetainedVisibleLink(**members)
    raise AuditIntegrityError("application graph has no actual constructor")


def application_result_tools(graph: ApplicationGraph, env_ref_names: frozenset[str]) -> dict[int, str]:
    from elspeth.web.composer.continuation_batch_data import _joint
    from elspeth.web.composer.continuation_p4_batch import _joined

    if type(graph) is not ApplicationGraph or type(graph.cuts) is not tuple:
        raise TypeError("application graph requires its actual owned roots")
    if (graph.selected_audit is None) != (graph.selected_dialect is None):
        raise AuditIntegrityError("active evidence graph lost its complete audit/dialect pair")
    if graph.selected_audit is not None and (
        type(graph.selected_audit) is not PortableDispatchAudit or type(graph.selected_dialect) is not ToolContractDialect
    ):
        raise TypeError("active evidence graph requires its actual portable audit/dialect")
    result_tools: dict[int, str] = {}
    for index, data in enumerate((graph.selected, *(cut.data for cut in graph.cuts))):
        if index == 0 and type(data) is RestoredBatchData and data.batch.phase in ("call_active", "call_ready", "dispatch_ready"):
            prepared = None
            if graph.selected_audit is not None and graph.selected_dialect is not None:
                prepared = (graph.selected_audit, graph.selected_dialect)
            selected = _joint(data, env_ref_names, prepared)
            result_tools.update(selected)
            continue
        if index == 0 and graph.selected_audit is not None:
            raise AuditIntegrityError("P4 evidence graph cannot acquire an active call audit")
        if type(data) is not RestoredBatchData or data.batch.phase not in ("p4_pending", "complete"):
            raise AuditIntegrityError("application graph lost its actual pending/completed batch")
        batch = data.batch
        if batch.p4 is not None or batch.current_call is not None:
            raise AuditIntegrityError("application graph cannot retain a committed witness/current call")
        if batch.phase == "p4_pending" and (batch.p4_receipt_json is not None or batch.p4_verification_failed):
            raise AuditIntegrityError("pending application invented a receipt/verification")
        if batch.phase == "complete" and type(batch.p4_receipt_json) is not str:
            raise AuditIntegrityError("completed application lost its exact historical receipt")
        pending = replace(batch, phase="p4_pending", p4_receipt_json=None, p4_verification_failed=False)
        _joined(RestoredBatchData(pending, data.accumulator, data.calls), env_ref_names=env_ref_names)
        plain = replace(pending, phase="dispatch_ready", p4_input_json=None)
        selected = _joint(RestoredBatchData(plain, data.accumulator, data.calls), env_ref_names)
        for identity, name in selected.items():
            if identity in result_tools and result_tools[identity] != name:
                raise AuditIntegrityError("shared application result has conflicting selected contracts")
            result_tools[identity] = name
    if type(graph.effects) is not PersistedTurnEffects or type(graph.recorder) is not RecorderSnapshot:
        raise TypeError("application graph lost actual evidence/recorder owners")
    for effects in (graph.effects, *(item for cut in graph.cuts for item in (cut.effects_before, cut.effects_after))):
        _admit_effects(effects, env_ref_names)
    encode_recorder_snapshot(graph.recorder)
    from elspeth.web.composer.continuation_tool_failure import require_retained_failure_links

    for data in (graph.selected, *(cut.data for cut in graph.cuts)):
        require_retained_failure_links(data, graph.recorder.invocations)
        failure = data.batch.retained_failure
        source = None if failure is None else failure.refusal_source
        if (
            source is not None
            and source.provider_call_id is not None
            and sum(call.call_id == source.provider_call_id for call in graph.recorder.llm_calls) != 1
        ):
            raise AuditIntegrityError("retained policy refusal lost its actual recorder cost correlation")
    for cut in graph.cuts:
        if type(cut) is not ApplicationGraphCut:
            raise TypeError("application graph contains another cut owner")
        if any(type(effects) is not PersistedTurnEffects for effects in (cut.effects_before, cut.effects_after)):
            raise TypeError("application cut lost its actual effects")
        for snapshot in (cut.recorder_before, cut.recorder_after):
            if type(snapshot) is not RecorderSnapshot:
                raise TypeError("application cut lost its actual recorder")
            encode_recorder_snapshot(snapshot)
    return result_tools


def encode_application_graph(graph: ApplicationGraph, *, env_ref_names: frozenset[str]) -> str:
    from elspeth.web.composer.continuation_batch_data import _decode_graph_values, _encode, _encode_values

    encoded = _encode(graph.selected, env_ref_names, application=graph)
    restored, result_tools = _decode_graph_values(encoded, env_ref_names, application=True)
    if (
        type(restored) is not ApplicationGraph
        or _encode_values(restored.selected, env_ref_names, result_tools, application=restored) != encoded
    ):
        raise AuditIntegrityError("application constructors changed the actual alias partition")
    return encoded


def decode_application_graph(encoded: str, *, env_ref_names: frozenset[str]) -> ApplicationGraph:
    from elspeth.web.composer.continuation_batch_data import _decode_graph_values, _encode_values

    restored, result_tools = _decode_graph_values(encoded, env_ref_names, application=True)
    if (
        type(restored) is not ApplicationGraph
        or _encode_values(restored.selected, env_ref_names, result_tools, application=restored) != encoded
    ):
        raise AuditIntegrityError("application graph lost complete values or aliases")
    return restored


def _admit_effects(value: PersistedTurnEffects, env_ref_names: frozenset[str]) -> None:
    if type(value) is not PersistedTurnEffects or any(
        type(state) is not CompositionState for state in (value.initial_state, value.latest_verified_state)
    ):
        raise TypeError("application effects require complete actual state owners")
    if (
        value.verification not in ("verified", "unavailable", "not_persisted")
        or value.initial_authored_state_hash != authored_state_hash(value.initial_state)
        or value.latest_authored_state_hash != authored_state_hash(value.latest_verified_state)
        or any(
            identity is not None and type(identity) is not str
            for identity in (value.initial_state_id, value.latest_verified_state_id, value.latest_committed_state_id)
        )
        or type(value.batches) is not tuple
        or type(value.committed_revision_ids) is not tuple
        or len(set(value.committed_revision_ids)) != len(value.committed_revision_ids)
    ):
        raise AuditIntegrityError("application effects changed their complete state/frontier facts")
    metadata: list[Any] = [
        value.initial_state_id,
        value.latest_verified_state_id,
        value.latest_committed_state_id,
        value.net_changed_fields,
        value.committed_revision_ids,
    ]
    for batch in value.batches:
        if (
            type(batch) is not PersistedBatchEffects
            or type(batch.version) is not int
            or batch.version < 1
            or not batch.inserted_state_ids
            or batch.inserted_state_ids[-1] != batch.state_id
            or len(set(batch.inserted_state_ids)) != len(batch.inserted_state_ids)
        ):
            raise AuditIntegrityError("application batch effects lost their actual inserted cohort")
        metadata.extend(
            (batch.assistant_message_id, batch.state_id, batch.tool_call_ids, batch.inserted_state_ids, batch.authored_state_hash)
        )
    require_no_credential_material(owned_json(metadata), surface="portable application effects", env_ref_names=env_ref_names)
