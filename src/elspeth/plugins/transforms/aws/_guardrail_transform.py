"""Shared fail-closed runtime for Bedrock Guardrail transforms."""

from __future__ import annotations

import hashlib
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from elspeth.contracts import Determinism, RunMode
from elspeth.contracts.audit_protocols import PluginAuditWriter
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.errors import FrameworkBugError, PluginRetryableError, TransformErrorCategory
from elspeth.contracts.events import TelemetryEvent
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.aws.guardrail_profiles import (
    check_bedrock_local_requirements,
    validate_guardrail_arn_binding,
    validate_guardrail_identifier,
    validate_guardrail_region,
    validate_guardrail_version,
)
from elspeth.plugins.transforms.aws.guardrails_client import (
    MAX_REQUEST_TEXT_CHARS,
    BedrockGuardrailsClient,
    GuardrailPartialCoverageError,
    GuardrailResponseError,
    GuardrailServiceError,
    GuardrailSource,
    build_bedrock_runtime_client,
)
from elspeth.plugins.transforms.aws.replay_sdk import DeferredAWSClient, ReplayOnlySDK
from elspeth.plugins.transforms.safety_utils import validate_fields_not_empty

# Default row-scoped budget for STARTING field calls (config: row_deadline_seconds).
# One ApplyGuardrail call is bounded by botocore at about 3 x (5 s connect + 15 s
# read) plus backoff, so a degraded endpoint costs ~1 min per field; without a
# bound a 32-field row could hold the engine's row thread for ~30 min. Fields left
# unscanned at the deadline fail the row closed as retry_timeout /
# row_deadline_exceeded (as azure/base.py does).
GUARDRAIL_ROW_DEADLINE_SECONDS = 300


class BedrockGuardrailTransformConfig(TransformDataConfig):
    """Explicit trained-operator binding shared by both transforms."""

    _component_type_exempt: ClassVar[bool] = True
    model_config: ClassVar[ConfigDict] = ConfigDict(**TransformDataConfig.model_config, hide_input_in_errors=True)

    guardrail_identifier: str = Field(
        min_length=1,
        max_length=2048,
        repr=False,
        description="Operator-installed Bedrock Guardrail identifier or ARN.",
    )
    guardrail_version: str = Field(
        min_length=1,
        max_length=32,
        repr=False,
        description="Published Bedrock Guardrail version used for evaluation.",
    )
    region: str = Field(min_length=1, max_length=64, repr=False, description="AWS region containing the Guardrail.")
    fields: list[str] = Field(
        min_length=1,
        max_length=32,
        description="Input row fields whose text is evaluated by the Guardrail.",
    )
    row_deadline_seconds: int = Field(
        GUARDRAIL_ROW_DEADLINE_SECONDS,
        gt=0,
        description=(
            "Seconds after a row starts within which each further field call must start; "
            "fields left unscanned at the deadline fail the row as row_deadline_exceeded."
        ),
    )

    @field_validator("guardrail_identifier")
    @classmethod
    def _identifier(cls, value: str) -> str:
        return validate_guardrail_identifier(value)

    @field_validator("guardrail_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return validate_guardrail_version(value)

    @field_validator("region")
    @classmethod
    def _region(cls, value: str) -> str:
        return validate_guardrail_region(value)

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: list[str]) -> list[str]:
        validate_fields_not_empty(value)
        if len(set(value)) != len(value):
            raise ValueError("fields must not contain duplicates")
        if any(len(field) > 256 for field in value):
            raise ValueError("field names must contain at most 256 characters")
        return value

    @model_validator(mode="after")
    def _arn_binding(self) -> Self:
        validate_guardrail_arn_binding(self.guardrail_identifier, region=self.region)
        return self


class BedrockGuardrailTransformBase(BaseTransform, ABC):
    """Common row scanning, audited call construction, and fail-closed aggregation."""

    determinism = Determinism.EXTERNAL_CALL
    passes_through_input = True
    plugin_version = "1.0.0"

    _required_filters: ClassVar[tuple[str, ...]]
    _detected_reason: ClassVar[TransformErrorCategory]
    _probe_field: ClassVar[str]

    @classmethod
    def check_web_local_requirements(cls) -> bool:
        return check_bedrock_local_requirements().available

    def __init__(self, config: dict[str, Any], cfg: BedrockGuardrailTransformConfig, schema_name: str) -> None:
        super().__init__(config)
        self._initialize_declared_input_fields(cfg)
        self._guardrail_identifier = cfg.guardrail_identifier
        self._guardrail_version = cfg.guardrail_version
        self._region = cfg.region
        self._fields = tuple(cfg.fields)
        # Every configured field must arrive string-valued or process() fails
        # the row closed — declare that so the DAG rejects a provably
        # non-string producer at build time (elspeth-b19dfe41fb).
        self.declared_string_input_fields = frozenset(cfg.fields)
        self._schema_config = cfg.schema_config
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)
        self.input_schema, self.output_schema = self._create_schemas(cfg.schema_config, schema_name)
        self._recorder: PluginAuditWriter | None = None
        self._run_id = ""
        self._telemetry_emit: Callable[[TelemetryEvent], None] = lambda _event: None
        self._sdk_client: Any | None = None
        self._row_deadline_seconds = cfg.row_deadline_seconds
        self._monotonic: Callable[[], float] = time.monotonic
        self._shutdown = threading.Event()

    @property
    @abstractmethod
    def _guardrail_source(self) -> GuardrailSource: ...

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        if ctx.landscape is None:
            raise FrameworkBugError("Bedrock Guardrail transforms require Landscape audit recording")
        self._recorder = ctx.landscape
        self._run_id = ctx.run_id
        self._telemetry_emit = ctx.telemetry_emit
        if self._sdk_client is None and ctx.run_mode is RunMode.REPLAY:
            self._sdk_client = ReplayOnlySDK()
        elif self._sdk_client is None and ctx.run_mode is RunMode.VERIFY:
            self._sdk_client = DeferredAWSClient(lambda: build_bedrock_runtime_client(self._region))
        elif self._sdk_client is None:
            self._sdk_client = build_bedrock_runtime_client(self._region)

    def close(self) -> None:
        # Stop any row still in its field loop before the next call starts.
        self._shutdown.set()
        sdk_client = self._sdk_client
        self._sdk_client = None
        if sdk_client is not None:
            sdk_client.close()

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "guardrail_identifier": "probeguardrail",
            "guardrail_version": "1",
            "region": "us-east-1",
            "fields": [cls._probe_field],
            "schema": {"mode": "observed"},
        }

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        return [self._augment_invariant_probe_row(probe, field_name=self._probe_field, value="safe content")]

    def execute_forward_invariant_probe(self, probe_rows: list[PipelineRow], ctx: Any) -> TransformResult:
        if len(probe_rows) != 1:
            raise FrameworkBugError("Bedrock Guardrail invariant probe requires exactly one row")

        class _SafeProbeSDK:
            def __init__(self, required_filters: tuple[str, ...]) -> None:
                self._required_filters = required_filters

            def apply_guardrail(self, **_kwargs: object) -> dict[str, object]:
                usage = {
                    "contentPolicyUnits": 1,
                    "contextualGroundingPolicyUnits": 0,
                    "sensitiveInformationPolicyFreeUnits": 0,
                    "sensitiveInformationPolicyUnits": 0,
                    "topicPolicyUnits": 0,
                    "wordPolicyUnits": 0,
                }
                filters = [{"type": name, "confidence": "NONE", "action": "NONE", "detected": False} for name in self._required_filters]
                return {
                    "usage": usage,
                    "action": "NONE",
                    "outputs": [],
                    "assessments": [{"contentPolicy": {"filters": filters}}],
                }

            def close(self) -> None:
                return None

        prior_recorder = self._recorder
        prior_run_id = self._run_id
        prior_telemetry = self._telemetry_emit
        prior_sdk = self._sdk_client
        try:
            self._recorder = ctx.landscape
            self._run_id = ctx.run_id
            self._telemetry_emit = ctx.telemetry_emit
            self._sdk_client = _SafeProbeSDK(self._required_filters)
            return self.process(probe_rows[0], ctx)
        finally:
            self._recorder = prior_recorder
            self._run_id = prior_run_id
            self._telemetry_emit = prior_telemetry
            self._sdk_client = prior_sdk

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        if self._recorder is None or self._sdk_client is None or not self._run_id:
            raise FrameworkBugError("Bedrock Guardrail transform used before on_start")
        if ctx.state_id is None:
            raise FrameworkBugError("Bedrock Guardrail transform requires a state_id")
        token_id = ctx.token.token_id if ctx.token is not None else None
        client = BedrockGuardrailsClient(
            member_token=ctx.require_member_token(),
            work_item=ctx.require_work_item(),
            execution=self._recorder,
            state_id=ctx.state_id,
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            guardrail_identifier=self._guardrail_identifier,
            guardrail_version=self._guardrail_version,
            region=self._region,
            audit_salt=hashlib.sha256(f"elspeth-bedrock-guardrail:{self._run_id}".encode()).digest(),
            source_audit_salt=(
                hashlib.sha256(f"elspeth-bedrock-guardrail:{ctx.call_mode_session.source_run_id}".encode()).digest()
                if ctx.call_mode_session is not None
                else None
            ),
            sdk_client=self._sdk_client,
            token_id=token_id,
            call_mode_session=ctx.call_mode_session,
        )

        row_started_at = self._monotonic()
        row_deadline = row_started_at + self._row_deadline_seconds
        # Validate every configured field before the first Bedrock call, as
        # azure/base.py does: an invalid later field must not let an earlier
        # field be sent, billed, and recorded before the row fails.
        texts: list[tuple[str, str]] = []
        for field_name in self._fields:
            if field_name not in row:
                return TransformResult.error({"reason": "missing_field", "field": field_name}, retryable=False)
            value = row[field_name]
            if type(value) is not str:
                return TransformResult.error(
                    {"reason": "non_string_field", "field": field_name, "actual_type": type(value).__name__},
                    retryable=False,
                )
            # The client refuses empty and over-long text before allocating a
            # call; reject it here as row data so it is never reported as a
            # Guardrail service failure with no call behind it.
            if not value:
                return TransformResult.error({"reason": "invalid_input", "field": field_name, "error_type": "empty_field"}, retryable=False)
            if len(value) > MAX_REQUEST_TEXT_CHARS:
                return TransformResult.error(
                    {"reason": "invalid_input", "field": field_name, "error_type": "field_too_long"}, retryable=False
                )
            texts.append((field_name, value))
        for field_name, value in texts:
            # Shutdown and row-deadline checks precede every call. The deadline
            # bounds when a call may START, not the in-flight call itself
            # (botocore's own timeouts bound that), mirroring azure/base.py.
            # Both end the row in an error, never a validated success.
            if self._shutdown.is_set() or (ctx.shutdown_event is not None and ctx.shutdown_event.is_set()):
                return TransformResult.error(
                    {
                        "reason": "shutdown_requested",
                        "field": field_name,
                        "error": "Bedrock Guardrail scan stopped because shutdown was requested",
                        "elapsed_seconds": self._monotonic() - row_started_at,
                        "max_seconds": self._row_deadline_seconds,
                    },
                    retryable=False,
                )
            if self._monotonic() >= row_deadline:
                return TransformResult.error(
                    {
                        "reason": "retry_timeout",
                        "field": field_name,
                        "error": "Bedrock Guardrail row deadline passed before this field was scanned",
                        "error_type": "row_deadline_exceeded",
                        "elapsed_seconds": self._monotonic() - row_started_at,
                        "max_seconds": self._row_deadline_seconds,
                    },
                    retryable=False,
                )
            try:
                decision = client.apply_guardrail(
                    text=value,
                    source=self._guardrail_source,
                    required_filters=self._required_filters,
                )
            except GuardrailPartialCoverageError as error:
                # Fail-closed with a distinct reason (operator decision
                # 2026-07-17): the guardrail scanned only part of the input,
                # so the row must not pass — but operators need to see the
                # coverage gate, not a service error.
                return TransformResult.error(
                    {
                        "reason": "guardrail_partial_coverage",
                        "field": field_name,
                        "error_type": "partial_coverage",
                        "coverage_key": error.coverage_key,
                        "guarded_units": error.guarded,
                        "total_units": error.total,
                    },
                    retryable=False,
                )
            except GuardrailResponseError:
                # The parser refused the provider's response; a retry would get
                # the same shape, so this is terminal for the row.
                return TransformResult.error(
                    {"reason": "api_call_failed", "field": field_name, "error_type": "malformed_response"},
                    retryable=False,
                )
            except GuardrailServiceError as error:
                # Only sanitized provider failures. A ValueError from the client
                # is an internal invariant (source / required_filters ClassVars)
                # and must crash, not become a per-row error.
                if error.retryable:
                    # Throttling / 5xx / transport after botocore's own retries:
                    # hand the row to the engine's RetryManager, which re-runs
                    # every field under a new attempt (TransformResult.error
                    # retryable=True is not read by the engine). Without a retry
                    # policy the engine records transient_error_no_retry.
                    raise PluginRetryableError(
                        f"Bedrock Guardrail service error on field {field_name!r} is retryable",
                        retryable=True,
                    ) from error
                return TransformResult.error(
                    {"reason": "api_call_failed", "field": field_name, "error_type": "service_error"},
                    retryable=False,
                )
            if decision.detected:
                return TransformResult.error(
                    {
                        "reason": self._detected_reason,
                        "field": field_name,
                        "categories": list(decision.matched_filters),
                        "error_type": "intervened" if decision.intervened else "detect_only",
                    },
                    retryable=False,
                )

        # Pass through unchanged, under the node's declared contract: the ONE
        # stamp writes the operator's declared fields onto it (ADR-050 Decision 2).
        return TransformResult.success(
            PipelineRow(row.to_dict(), self._align_output_contract(self._apply_declared_output_field_contracts(row.contract))),
            success_reason={"action": "validated", "metadata": {"fields_checked": len(self._fields)}},
        )
