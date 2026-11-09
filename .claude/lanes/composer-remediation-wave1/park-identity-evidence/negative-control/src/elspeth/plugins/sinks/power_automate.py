"""Recoverable, per-member publication to an endpoint-fixed Power Automate flow."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Self
from urllib.parse import urlsplit

from elspeth.contracts import Determinism, PluginSchema
from elspeth.contracts.contexts import SinkContext
from elspeth.contracts.diversion import SinkWriteResult
from elspeth.contracts.enums import CallType
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effect_http import (
    HTTPSinkEffectCapability,
    SinkEffectHTTPPostFactory,
    SinkEffectHTTPPostRequest,
    SinkEffectHTTPPostResponse,
)
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    MemberSinkEffectCapability,
    ResolvedSinkEffectMode,
    RestrictedSinkEffectContext,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
    SinkEffectInspection,
    SinkEffectInspectionMode,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileResult,
)
from elspeth.core.config import sanitize_node_config_for_audit
from elspeth.plugins.infrastructure.base import BaseSink
from elspeth.plugins.infrastructure.power_automate import (
    PROTOCOL,
    PowerAutomateApplied,
    PowerAutomateEffectResponse,
    PowerAutomateNotApplied,
    PowerAutomateRejected,
    PowerAutomateSinkConfig,
    parse_effect_response,
    selected_data_hash,
)
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sinks._diversion_attribution import build_diversion_attribution

if TYPE_CHECKING:
    from elspeth.plugins.infrastructure.power_automate_nonlive import (
        ArchivedPowerAutomateOptions,
        ArchivedPowerAutomateSinkConfig,
        DeferredPowerAutomateCredential,
    )


class PowerAutomateSink(BaseSink, MemberSinkEffectCapability, HTTPSinkEffectCapability):
    """Publish each engine delivery once under the flow's atomic dedup contract."""

    name = "power_automate"
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:760d8ed428da2285"
    determinism = Determinism.EXTERNAL_CALL
    config_model = PowerAutomateSinkConfig
    idempotent = True
    supports_resume = True
    effect_protocol_version = SINK_EFFECT_PROTOCOL_VERSION
    effect_call_type = CallType.HTTP
    supported_effect_modes = frozenset({"write"})
    supported_effect_input_kinds = frozenset({SinkEffectInputKind.PIPELINE_MEMBERS})
    usage_when_to_use: str | None = (
        "Use an HTTP-triggered flow with atomic delivery-ID deduplication, read-only status and durable exact receipts."
    )
    usage_when_not_to_use: str | None = (
        "Do not use trigger acknowledgements, non-deduplicated actions, arbitrary destinations or audit export."
    )
    capability_tags: tuple[str, ...] = ("power-automate", "http", "flow", "recoverable")
    example_use: str | None = """sinks:
  publish:
    plugin: power_automate
    on_write_failure: discard
    options:
      auth: {method: managed_identity, client_id: example-user-assigned-client}
      trigger_url: https://flow-endpoint.example.org/workflows/example/triggers/manual/paths/invoke
      allowed_origin: https://flow-endpoint.example.org
      fields: [record_id, result]
      schema: {mode: flexible, fields: ["record_id: str", "result: str"]}
"""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = PowerAutomateSinkConfig.from_dict(config, plugin_name=self.name)
        safe = deep_thaw(sanitize_node_config_for_audit(config, plugin_name=self.name))
        if not isinstance(safe, dict):
            raise FrameworkBugError("power_automate_safe_config_invalid")
        self._initialize(safe, cfg)

    def _initialize(self, config: dict[str, Any], cfg: PowerAutomateSinkConfig | ArchivedPowerAutomateSinkConfig) -> None:
        super().__init__(config)
        self._cfg = cfg
        self._initial_runtime_options = cfg.model_dump()
        self._safe_config_fingerprint = stable_hash(config)
        self._fields = tuple(cfg.fields)
        self._schema_config = cfg.schema_config
        self.input_schema: type[PluginSchema] = create_schema_from_config(cfg.schema_config, "PowerAutomateSinkRow", allow_coercion=False)
        self.declared_required_fields = cfg.schema_config.get_effective_required_fields()
        host = urlsplit(cfg.allowed_origin).netloc
        self._effect_target = f"power-automate://{host}/binding/{self._safe_config_fingerprint}"

    @classmethod
    def from_archived_options(
        cls, options: ArchivedPowerAutomateOptions, *, credential: DeferredPowerAutomateCredential | None = None
    ) -> Self:
        from elspeth.plugins.infrastructure.power_automate_nonlive import ArchivedPowerAutomateOptions, ArchivedPowerAutomateSinkConfig

        if type(options) is not ArchivedPowerAutomateOptions or options.component_type != "sink":
            raise TypeError("power_automate_archived_sink_options_required")
        if credential is not None:
            raise TypeError("power_automate_nonlive_sink_credential_refused")
        if not isinstance(options.runtime_spec, ArchivedPowerAutomateSinkConfig):
            raise TypeError("power_automate_archived_sink_spec_required")
        safe = deep_thaw(options.safe_options)
        if not isinstance(safe, dict):
            raise FrameworkBugError("power_automate_safe_config_invalid")
        instance = cls.__new__(cls)
        instance._initialize(safe, options.runtime_spec)
        return instance

    @property
    def safe_config_fingerprint(self) -> str:
        self._assert_configuration()
        return self._safe_config_fingerprint

    def make_http_post_factory(self) -> SinkEffectHTTPPostFactory:
        """Compose live endpoint authority without credentials, clients or I/O."""
        from elspeth.plugins.infrastructure.clients.power_automate import PowerAutomateHTTPPostFactory

        self._assert_configuration()
        if type(self._cfg) is not PowerAutomateSinkConfig:
            raise FrameworkBugError("power_automate_nonlive_factory_refused")
        return PowerAutomateHTTPPostFactory(self._cfg, safe_config=self.config)

    def _assert_configuration(self) -> None:
        if stable_hash(self.config) != self._safe_config_fingerprint or self._cfg.model_dump() != self._initial_runtime_options:
            raise ValueError("power_automate_configuration_changed")

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        del cls
        return {
            "auth": {"method": "managed_identity", "client_id": "example-user-assigned-client"},
            "trigger_url": "https://flow-endpoint.example.org/workflows/example/triggers/manual/paths/invoke",
            "allowed_origin": "https://flow-endpoint.example.org",
            "fields": ["record_id", "result"],
            "schema": {"mode": "flexible", "fields": ["record_id: str", "result: str"]},
        }

    @classmethod
    def _resolve_sink_effect_mode(
        cls, config: Mapping[str, object], *, purpose: SinkEffectExecutionPurpose
    ) -> ResolvedSinkEffectMode | None:
        del cls, config
        return None if purpose is SinkEffectExecutionPurpose.AUDIT_EXPORT else ResolvedSinkEffectMode("write")

    def _validate_sink_effect_capability_configuration(self, *, mode: str, required_input_kind: SinkEffectInputKind) -> None:
        self._assert_configuration()
        if mode != "write" or required_input_kind is not SinkEffectInputKind.PIPELINE_MEMBERS:
            raise ValueError("power_automate_effect_mode_refused")

    def config_named_input_columns(self) -> frozenset[str]:
        return super().config_named_input_columns() | frozenset(self._fields)

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is not None:
            return None
        return PluginAssistance(
            plugin_name=cls.name,
            issue_code=None,
            summary="Publishes selected fields to a recoverable HTTP-triggered Power Automate flow.",
            composer_hints=(
                "Select explicit required input fields; only these fields are sent.",
                "Use the documented status/write protocol with atomic delivery-ID deduplication and durable receipts.",
                "The operator must approve allowed_origin and independently wire credentials; authored origin grants no permission.",
                "Only a sticky validated row rejection diverts; uncertain responses abort for reconciliation.",
            ),
        )

    def inspect_effect(self, request: SinkEffectInspectionRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectInspection:
        del ctx
        self._assert_configuration()
        if request.input_kind is not SinkEffectInputKind.PIPELINE_MEMBERS:
            raise TypeError("power_automate_pipeline_members_required")
        return SinkEffectInspection(SinkEffectInspectionMode.NO_INSPECTION_REQUIRED, "no-inspection-required:v1", {})

    def _material(
        self, effect_id: str, effect_input: SinkEffectPipelineMembersInput
    ) -> tuple[ArtifactDescriptor, str, str, tuple[dict[str, object], ...]]:
        self._assert_configuration()
        payloads = tuple({name: deep_thaw(member.row[name]) for name in self._fields} for member in effect_input.members)
        bindings = []
        delivery_ids: set[str] = set()
        for member, payload in zip(effect_input.members, payloads, strict=True):
            if member.member_effect_id is None:
                raise ValueError("power_automate_member_identity_required")
            if member.member_effect_id in delivery_ids:
                raise ValueError("power_automate_duplicate_member_identity")
            delivery_ids.add(member.member_effect_id)
            bindings.append(
                {"delivery_id": member.member_effect_id, "ordinal": member.ordinal, "payload_sha256": selected_data_hash(payload)}
            )
        canonical = canonical_json(payloads).encode("utf-8")
        payload_hash = stable_hash(payloads)
        descriptor = ArtifactDescriptor(
            artifact_type="webhook",
            path_or_uri=self._effect_target,
            content_hash=payload_hash,
            size_bytes=len(canonical),
            metadata=MappingProxyType({"row_count": len(payloads), "protocol": PROTOCOL, "fields": self._fields}),
        )
        plan_hash = stable_hash(
            {
                "schema": "power-automate-member-effect-plan-v1",
                "effect_id": effect_id,
                "target": self._effect_target,
                "safe_config_hash": self._safe_config_fingerprint,
                "fields": self._fields,
                "members": bindings,
                "payload_hash": payload_hash,
                "size_bytes": len(canonical),
            }
        )
        return descriptor, payload_hash, plan_hash, payloads

    def prepare_effect(self, request: SinkEffectPrepareRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectPlan:
        del ctx
        if type(request.effect_input) is not SinkEffectPipelineMembersInput:
            raise TypeError("power_automate_pipeline_members_required")
        if request.inspection.mode is not SinkEffectInspectionMode.NO_INSPECTION_REQUIRED:
            raise ValueError("power_automate_inspection_divergent")
        descriptor, payload_hash, plan_hash, _ = self._material(request.effect_id, request.effect_input)
        return SinkEffectPlan(
            effect_id=request.effect_id,
            protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            descriptor_mode=SinkEffectDescriptorMode.PRECOMPUTED,
            inspection_mode=request.inspection.mode,
            target=self._effect_target,
            plan_hash=plan_hash,
            payload_hash=payload_hash,
            expected_descriptor=descriptor,
            safe_evidence={
                "schema": "power-automate-member-effect-plan-v1",
                "safe_config_hash": self._safe_config_fingerprint,
                "member_count": len(request.effect_input.members),
            },
        )

    def _validate_member(
        self, plan: SinkEffectPlan, member: SinkEffectMember, effect_input: SinkEffectPipelineMembersInput
    ) -> tuple[dict[str, object], ArtifactDescriptor]:
        descriptor, payload_hash, plan_hash, payloads = self._material(plan.effect_id, effect_input)
        if (
            plan.protocol_version != SINK_EFFECT_PROTOCOL_VERSION
            or plan.input_kind is not SinkEffectInputKind.PIPELINE_MEMBERS
            or plan.descriptor_mode is not SinkEffectDescriptorMode.PRECOMPUTED
            or plan.inspection_mode is not SinkEffectInspectionMode.NO_INSPECTION_REQUIRED
            or plan.target != self._effect_target
            or plan.plan_hash != plan_hash
            or plan.payload_hash != payload_hash
            or plan.expected_descriptor != descriptor
            or plan.safe_evidence
            != {
                "schema": "power-automate-member-effect-plan-v1",
                "safe_config_hash": self._safe_config_fingerprint,
                "member_count": len(effect_input.members),
            }
        ):
            raise ValueError("power_automate_plan_divergent")
        if member.ordinal >= len(effect_input.members) or effect_input.members[member.ordinal] != member:
            raise ValueError("power_automate_member_divergent")
        return payloads[member.ordinal], descriptor

    def _post(
        self,
        *,
        operation: Literal["status", "write"],
        plan: SinkEffectPlan,
        member: SinkEffectMember,
        payload: dict[str, object],
        ctx: RestrictedSinkEffectContext,
    ) -> tuple[PowerAutomateEffectResponse, SinkEffectHTTPPostResponse]:
        if ctx.http_post is None:
            raise FrameworkBugError("power_automate_attempt_http_missing")
        if member.member_effect_id is None:
            raise FrameworkBugError("power_automate_member_identity_required")
        body: dict[str, object] = {
            "protocol": PROTOCOL,
            "operation": operation,
            "delivery_id": member.member_effect_id,
            "payload_sha256": selected_data_hash(payload),
        }
        if operation == "write":
            body["data"] = payload
        if len(canonical_json(body).encode("utf-8")) > self._cfg.max_request_body_bytes:
            raise ValueError("power_automate_request_too_large")
        response = ctx.http_post.post_json(SinkEffectHTTPPostRequest(body))
        if response.status_code != 200:
            raise RuntimeError("power_automate_http_status_refused")
        if response.content_type is None or response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise RuntimeError("power_automate_media_type_refused")
        if len(response.body) > self._cfg.max_response_body_bytes:
            raise RuntimeError("power_automate_response_too_large")
        if operation not in ("status", "write"):
            raise FrameworkBugError("power_automate_operation_refused")
        result = parse_effect_response(
            response.body,
            expected_delivery_id=member.member_effect_id,
            expected_payload_sha256=selected_data_hash(payload),
            operation=operation,
        )
        return result, response

    @staticmethod
    def _evidence(plan: SinkEffectPlan, result: PowerAutomateEffectResponse, response: SinkEffectHTTPPostResponse) -> dict[str, object]:
        evidence: dict[str, object] = {
            "schema": "power-automate-member-effect-result-v1",
            "effect_id": plan.effect_id,
            "plan_hash": plan.plan_hash,
            "state": result.state,
            "delivery_id": result.delivery_id,
            "payload_sha256": result.payload_sha256,
            "call_id": response.call_id,
            "request_ref": response.request_ref,
            "response_ref": response.response_ref,
        }
        if isinstance(result, PowerAutomateApplied):
            evidence.update(receipt_id=result.receipt_id, flow_run_id=result.flow_run_id)
        if isinstance(result, PowerAutomateRejected):
            evidence["reason_code"] = result.reason_code
        return evidence

    def reconcile_member_effect(
        self, plan: SinkEffectPlan, member: SinkEffectMember, effect_input: SinkEffectPipelineMembersInput, ctx: RestrictedSinkEffectContext
    ) -> SinkEffectReconcileResult:
        payload, descriptor = self._validate_member(plan, member, effect_input)
        result, response = self._post(operation="status", plan=plan, member=member, payload=payload, ctx=ctx)
        evidence = self._evidence(plan, result, response)
        if isinstance(result, PowerAutomateApplied):
            return SinkEffectReconcileResult.applied(descriptor, evidence=evidence)
        if isinstance(result, PowerAutomateNotApplied | PowerAutomateRejected):
            return SinkEffectReconcileResult.not_applied(evidence=evidence)
        return SinkEffectReconcileResult.unknown(evidence=evidence)

    def commit_member_effect(
        self, plan: SinkEffectPlan, member: SinkEffectMember, effect_input: SinkEffectPipelineMembersInput, ctx: RestrictedSinkEffectContext
    ) -> SinkEffectCommitResult:
        payload, descriptor = self._validate_member(plan, member, effect_input)
        result, response = self._post(operation="write", plan=plan, member=member, payload=payload, ctx=ctx)
        evidence = self._evidence(plan, result, response)
        if isinstance(result, PowerAutomateApplied):
            return SinkEffectCommitResult(descriptor, evidence, tuple(item.ordinal for item in effect_input.members), ())
        if isinstance(result, PowerAutomateRejected):
            reason = f"Power Automate rejected member: {result.reason_code}"
            row = deep_thaw(member.row)
            if not isinstance(row, dict):
                raise FrameworkBugError("power_automate_member_row_invalid")
            self._divert_row(row, row_index=member.ordinal, reason=reason)
            evidence["diversion_attribution"] = [build_diversion_attribution(ordinal=member.ordinal, reason=reason).as_mapping()]
            return SinkEffectCommitResult(
                descriptor,
                evidence,
                tuple(item.ordinal for item in effect_input.members if item.ordinal != member.ordinal),
                (member.ordinal,),
            )
        raise RuntimeError("power_automate_remote_outcome_unknown")

    def commit_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectCommitResult:
        del plan, ctx
        raise FrameworkBugError("power_automate_member_coordination_required")

    def reconcile_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectReconcileResult:
        del plan, ctx
        raise FrameworkBugError("power_automate_member_coordination_required")

    def write(self, rows: list[dict[str, Any]], ctx: SinkContext) -> SinkWriteResult:
        del rows, ctx
        raise RuntimeError("power_automate_member_coordination_required")

    def configure_for_resume(self) -> None:
        self._assert_configuration()

    def flush(self) -> None:
        """No local staging or external effects."""

    def close(self) -> None:
        """Transport resources belong to each coordinator-scoped attempt."""
