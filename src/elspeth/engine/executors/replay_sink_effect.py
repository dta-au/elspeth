"""Audit-only sink effects for replay and verify runs.

The configured sink is never handed to the effect coordinator. Its boundary
contract is checked by SinkExecutor, while this adapter records a virtual
no-publication effect for the would-be sink members. The complete member set
is compared with the source run before the run can finish successfully.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256

from elspeth.contracts.enums import CallType, NodeType
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    RestrictedSinkEffectContext,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectInspection,
    SinkEffectInspectionMode,
    SinkEffectInspectionRequest,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileResult,
    SinkEffectState,
)
from elspeth.core.landscape.factory import RecorderFactory


class VirtualReplaySinkEffect:
    """Finalize the exact sink-boundary members without external publication."""

    effect_call_type = CallType.FILESYSTEM

    def __init__(self, *, source_run_id: str, sink_node_id: str) -> None:
        self._source_run_id = source_run_id
        self._sink_node_id = sink_node_id

    def inspect_effect(self, request: SinkEffectInspectionRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectInspection:
        del request, ctx
        return SinkEffectInspection(
            mode=SinkEffectInspectionMode.NO_INSPECTION_REQUIRED,
            reference="virtual-replay-sink:v1",
            evidence={"schema": "virtual-replay-sink-inspection-v1"},
        )

    def prepare_effect(self, request: SinkEffectPrepareRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectPlan:
        if not isinstance(request.effect_input, SinkEffectPipelineMembersInput):
            raise OrchestrationInvariantError("virtual replay sink requires pipeline members")
        members = request.effect_input.members
        target = f"virtual-replay://{ctx.run_id}/{self._sink_node_id}"
        payload_hash = stable_hash([member.payload_hash for member in members])
        evidence = {
            "accepted_ordinals": [member.ordinal for member in members],
            "diverted_ordinals": [],
            "member_count": len(members),
            "publication_kind": "virtual",
            "schema": "virtual-replay-sink-plan-v1",
            "source_run_id": self._source_run_id,
        }
        return SinkEffectPlan(
            effect_id=request.effect_id,
            protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
            input_kind=request.input_kind,
            descriptor_mode=SinkEffectDescriptorMode.NO_PUBLICATION,
            inspection_mode=request.inspection.mode,
            target=target,
            plan_hash=stable_hash({"evidence": evidence, "payload_hash": payload_hash, "target": target}),
            payload_hash=payload_hash,
            expected_descriptor=ArtifactDescriptor(
                artifact_type="file",
                path_or_uri=f"file://virtual-replay/{ctx.run_id}/{self._sink_node_id}/{request.effect_id}",
                content_hash=sha256(b"").hexdigest(),
                size_bytes=0,
            ),
            safe_evidence=evidence,
        )

    def commit_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectCommitResult:
        del plan, ctx
        raise OrchestrationInvariantError("virtual replay sink cannot publish")

    def reconcile_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectReconcileResult:
        del plan, ctx
        raise OrchestrationInvariantError("virtual replay sink cannot reconcile publication")


def verify_virtual_sink_members(factory: RecorderFactory, *, source_run_id: str, current_run_id: str) -> None:
    """Require exact source/current sink-boundary membership before run success.

    Ingest sequence ties duplicate payloads to source rows; payload hashes are
    the type-faithful canonical values the sink-effect ledger sealed. Token and
    row UUIDs intentionally differ between executions.
    """
    source_effects = factory.execution.sink_effects.get_effects_for_run(source_run_id)
    current_effects = factory.execution.sink_effects.get_effects_for_run(current_run_id)
    if any(effect.state is not SinkEffectState.FINALIZED for effect in source_effects):
        raise AuditIntegrityError("replay source run contains a non-finalized sink effect")
    if any(effect.state is not SinkEffectState.FINALIZED or effect.publication_performed is not False for effect in current_effects):
        raise AuditIntegrityError("replay run contains an incomplete or published sink effect")

    source_nodes = {node.node_id for node in factory.data_flow.get_nodes(source_run_id) if node.node_type is NodeType.SINK}
    current_nodes = {node.node_id for node in factory.data_flow.get_nodes(current_run_id) if node.node_type is NodeType.SINK}
    if source_nodes != current_nodes:
        raise OrchestrationInvariantError("replay sink graph differs from the source run")

    def identity(run_id: str) -> Counter[tuple[str, str, int, str, str]]:
        members = factory.execution.sink_effects.get_members_for_run(run_id)
        return Counter(
            (
                member.sink_node_id,
                member.role.value,
                member.ingest_sequence,
                member.payload_hash,
                member.prepared_disposition or "unprepared",
            )
            for member in members
        )

    source_members = identity(source_run_id)
    current_members = identity(current_run_id)
    if source_members != current_members:
        raise OrchestrationInvariantError("replay sink output differs from the source run")
