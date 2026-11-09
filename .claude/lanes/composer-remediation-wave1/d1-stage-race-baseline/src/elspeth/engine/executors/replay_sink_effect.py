"""Audit-only sink effects for replay and verify runs.

The configured sink is never handed to the effect coordinator. Its boundary
contract is checked by SinkExecutor, while this adapter records a virtual
no-publication effect for the would-be sink members. The complete member set
is compared with the source run before the run can finish successfully.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from hashlib import sha256
from types import MappingProxyType

from elspeth.contracts.enums import CallType, NodeType, RunMode, RunStatus
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError, VerificationMismatchError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    RestrictedSinkEffectContext,
    SinkEffectAttemptAction,
    SinkEffectAttemptState,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectInputKind,
    SinkEffectInspection,
    SinkEffectInspectionMode,
    SinkEffectInspectionRequest,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileResult,
    SinkEffectRole,
    SinkEffectState,
)
from elspeth.core.landscape.execution.sink_effect_attempt_results import decode_sink_effect_returned_result
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator


class VirtualReplaySinkEffect:
    """Finalize the exact sink-boundary members without external publication."""

    effect_call_type = CallType.FILESYSTEM
    _DISPOSITION_CACHE_LIMIT = 4

    def __init__(
        self, *, factory: RecorderFactory, source_run_id: str, sink_node_id: str, role: SinkEffectRole = SinkEffectRole.PRIMARY
    ) -> None:
        self._factory = factory
        self._source_run_id = source_run_id
        self._sink_node_id = sink_node_id
        self._role = role

    def _source_dispositions(self) -> Mapping[tuple[int, str, str], tuple[str, str | None, str | None]]:
        """Reuse validated source outcomes across this factory's virtual effects."""
        scope = (self._sink_node_id, self._role)
        with self._factory._replay_sink_dispositions_lock:
            cache = self._factory._replay_sink_dispositions
            if self._source_run_id in cache:
                cached = cache[self._source_run_id]
                cache.move_to_end(self._source_run_id)
                if scope in cached:
                    return cached[scope]
                return {}
            source = self._factory.run_lifecycle.get_run(self._source_run_id)
            dispositions = self._read_run_dispositions(self._factory, self._source_run_id)
            # Only terminal runs are immutable source evidence. Direct callers
            # can inspect an active run, but may not cache a moving ledger.
            if (
                source is not None
                and source.completed_at is not None
                and source.status
                in (
                    RunStatus.COMPLETED,
                    RunStatus.COMPLETED_WITH_FAILURES,
                    RunStatus.EMPTY,
                )
            ):
                cached = MappingProxyType({key: MappingProxyType(members) for key, members in dispositions.items()})
                cache[self._source_run_id] = cached
                if len(cache) > self._DISPOSITION_CACHE_LIMIT:
                    cache.popitem(last=False)
                if scope in cached:
                    return cached[scope]
                return {}
            if scope in dispositions:
                return dispositions[scope]
            return {}

    @staticmethod
    def _read_run_dispositions(
        factory: RecorderFactory, run_id: str
    ) -> dict[tuple[str, SinkEffectRole], dict[tuple[int, str, str], tuple[str, str | None, str | None]]]:
        """Validate every pipeline effect once and index its member evidence."""
        repository = factory.execution.sink_effects
        dispositions_by_scope: dict[tuple[str, SinkEffectRole], dict[tuple[int, str, str], tuple[str, str | None, str | None]]] = {}
        for effect in repository.get_effects_for_run(run_id):
            if effect.input_kind is not SinkEffectInputKind.PIPELINE_MEMBERS:
                continue
            if effect.state is not SinkEffectState.FINALIZED:
                raise AuditIntegrityError("replay source run contains a non-finalized sink effect")
            scope = (effect.sink_node_id, effect.role)
            if scope not in dispositions_by_scope:
                dispositions_by_scope[scope] = {}
            dispositions = dispositions_by_scope[scope]
            attribution: dict[int, tuple[str, str]] = {}

            def merge_attribution(evidence: Mapping[str, object], attribution: dict[int, tuple[str, str]]) -> None:
                if "diversion_attribution" not in evidence:
                    return
                raw = deep_thaw(evidence["diversion_attribution"])
                if type(raw) is not list:
                    raise AuditIntegrityError("source sink diversion attribution must be a list")
                for item in raw:
                    if type(item) is not dict or set(item) != {"ordinal", "reason_hash", "error_hash"}:
                        raise AuditIntegrityError("source sink diversion attribution has a divergent field set")
                    ordinal, reason_hash, error_hash = item["ordinal"], item["reason_hash"], item["error_hash"]
                    if (
                        type(ordinal) is not int
                        or ordinal < 0
                        or type(reason_hash) is not str
                        or len(reason_hash) != 64
                        or any(char not in "0123456789abcdef" for char in reason_hash)
                        or type(error_hash) is not str
                        or len(error_hash) != 16
                        or any(char not in "0123456789abcdef" for char in error_hash)
                    ):
                        raise AuditIntegrityError("source sink diversion attribution is invalid")
                    value = (reason_hash, error_hash)
                    if ordinal in attribution and attribution[ordinal] != value:
                        raise AuditIntegrityError("source sink diversion attribution sources diverge")
                    attribution[ordinal] = value

            merge_attribution(SinkEffectCoordinator._load_plan(effect).safe_evidence, attribution)
            for attempt in repository.get_attempts(effect.effect_id):
                if (
                    attempt.state is SinkEffectAttemptState.RETURNED
                    and attempt.action in (SinkEffectAttemptAction.COMMIT, SinkEffectAttemptAction.RECONCILE)
                    and attempt.evidence_json is not None
                ):
                    merge_attribution(decode_sink_effect_returned_result(attempt.action, attempt.evidence_json).evidence, attribution)
            members = repository.get_members(effect.effect_id)
            if set(attribution) != {member.ordinal for member in members if member.prepared_disposition == "diverted"}:
                raise AuditIntegrityError("source sink effect is missing durable diversion attribution")
            for member in members:
                if member.prepared_disposition not in ("accepted", "diverted"):
                    raise AuditIntegrityError("source sink member has no prepared disposition")
                reason_hash, error_hash = attribution[member.ordinal] if member.prepared_disposition == "diverted" else (None, None)
                # Commit-time diversions retain attribution in the returned
                # attempt; the member's prepare-time reason may be absent.
                if member.reason_hash is not None and member.reason_hash != reason_hash:
                    raise AuditIntegrityError("source sink diversion reason disagrees with durable member")
                key = (member.ingest_sequence, member.lineage_hash, member.payload_hash)
                disposition = (member.prepared_disposition, reason_hash, error_hash)
                if key in dispositions and dispositions[key] != disposition:
                    raise AuditIntegrityError("source sink members have ambiguous dispositions")
                dispositions[key] = disposition
        return dispositions_by_scope

    def inspect_effect(self, request: SinkEffectInspectionRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectInspection:
        del request, ctx
        return SinkEffectInspection(
            mode=SinkEffectInspectionMode.NO_INSPECTION_REQUIRED,
            reference="virtual-replay-sink:v1",
            evidence={"schema": "virtual-replay-sink-inspection-v1"},
        )

    def prepare_effect(self, request: SinkEffectPrepareRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectPlan:
        if type(request.effect_input) is not SinkEffectPipelineMembersInput:
            raise OrchestrationInvariantError("virtual replay sink requires pipeline members")
        members = request.effect_input.members
        source_dispositions = self._source_dispositions()
        accepted: list[int] = []
        diverted: list[int] = []
        attribution: list[dict[str, object]] = []
        for member in members:
            key = (member.ingest_sequence, member.lineage_hash, member.payload_hash)
            if key not in source_dispositions:
                # New or changed output remains visible to the complete member
                # comparison, which reports the mode-specific drift verdict.
                accepted.append(member.ordinal)
                continue
            disposition, reason_hash, error_hash = source_dispositions[key]
            if disposition == "accepted":
                accepted.append(member.ordinal)
            else:
                diverted.append(member.ordinal)
                attribution.append({"ordinal": member.ordinal, "reason_hash": reason_hash, "error_hash": error_hash})
        target = f"virtual-replay://{ctx.run_id}/{self._sink_node_id}"
        payload_hash = stable_hash([member.payload_hash for member in members])
        evidence = {
            "accepted_ordinals": accepted,
            "diverted_ordinals": diverted,
            "diversion_attribution": attribution,
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


def verify_virtual_sink_members(factory: RecorderFactory, *, source_run_id: str, current_run_id: str, mode: RunMode) -> None:
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
        if mode is RunMode.VERIFY:
            raise VerificationMismatchError("Verify sink output differs from the source run")
        raise OrchestrationInvariantError("replay sink output differs from the source run")

    # Rebuild from durable evidence at the terminal boundary. This also
    # detects source attribution changed after an earlier cached preparation.
    source_dispositions = VirtualReplaySinkEffect._read_run_dispositions(factory, source_run_id)
    current_dispositions = VirtualReplaySinkEffect._read_run_dispositions(factory, current_run_id)
    if source_dispositions != current_dispositions:
        if mode is RunMode.VERIFY:
            raise VerificationMismatchError("Verify sink disposition evidence differs from the source run")
        raise OrchestrationInvariantError("replay sink disposition evidence differs from the source run")
