"""Bounded, discovery-only planner for canonical pipeline proposals.

The planner deliberately has no mutation or persistence authority.  It can
read through a pinned core/blob/secret discovery palette, validate a complete
``set_pipeline`` candidate through the production candidate builder, settle
inline source custody only after that candidate is acceptable, and return an
immutable :class:`PipelineProposal`.  Publishing the proposal remains a route
or session-service responsibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal, NotRequired, Protocol, TypedDict, cast, final
from uuid import UUID

import structlog
from litellm.exceptions import APIError as LiteLLMAPIError
from litellm.exceptions import AuthenticationError as LiteLLMAuthError
from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Engine

from elspeth.contracts.blobs import BlobGuidedOperationWriteFence
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.composer_progress import ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, dispatch_with_audit
from elspeth.web.composer.authority_hashing import project_composer_authority_payload
from elspeth.web.composer.bounded_json import JsonBoundaryError, bounded_json_loads, require_bounded_text
from elspeth.web.composer.capability_skill import (
    PLANNER_DISCOVERY_TOOL_NAMES,
    PLANNER_TERMINAL_TOOL_NAME,
    PlannerCapabilityManifest,
    build_planner_capability_manifest,
)
from elspeth.web.composer.discovery_cache import pydantic_default, serialize_tool_result
from elspeth.web.composer.guided.deferred_intents import DeferredIntentClaimError
from elspeth.web.composer.llm_response_parsing import (
    apply_anthropic_cache_markers,
    attach_llm_calls,
    build_llm_call_record,
    supports_anthropic_prompt_cache_markers,
)
from elspeth.web.composer.pipeline_custody import finalize_pipeline_custody, prepare_pipeline_custody
from elspeth.web.composer.pipeline_proposal import PipelineProposal, PlannerSurface, ProposalBase, reviewed_anchor_hash
from elspeth.web.composer.planner_authoring_aids import build_planner_authoring_aids
from elspeth.web.composer.progress import (
    emit_progress,
    model_call_progress_event,
    tool_batch_progress_event,
    tool_completed_progress_event,
    tool_started_progress_event,
)
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.reviewed_source_authority import resolve_reviewed_source_authority
from elspeth.web.composer.state import (
    CompositionState,
    RouteDestinationFactDict,
    ValidationEntry,
    ValidationSummary,
    coalesce_reachability_facts,
    gate_condition_is_constant,
    route_destination_facts,
)
from elspeth.web.composer.tools._common import RuntimePreflight, ToolContext, ToolResult
from elspeth.web.composer.tools._dispatch import (
    execute_discovery_tool_with_context,
    get_tool_definitions,
)
from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate, canonicalize_authored_node_review_requirements
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_PLANNER_DISCOVERY_TOOL_NAME_SET: Final[frozenset[str]] = frozenset(PLANNER_DISCOVERY_TOOL_NAMES)
_TERMINAL_TOOL_NAME: Final[str] = PLANNER_TERMINAL_TOOL_NAME


class _Completion(Protocol):
    async def __call__(self, **kwargs: Any) -> Any: ...


class PipelinePlannerError(RuntimeError):
    """Leak-safe failure raised when the bounded planner cannot continue.

    ``detail_codes`` carries the closed, leak-safe validation error codes of
    the last candidate rejection when the failure is a repair/composition
    exhaustion — the discriminant a live 5xx investigation needs, recorded on
    the durable failure disposition so it never requires a temp diagnostic.
    Empty for non-rejection failures (timeout, provider error, ...).

    ``unproducible_output_fields`` carries the reviewed output fields no
    reviewed source declares or observes, when the request was planned with a
    known gap (R2-F4). Without it an exhausted guided plan answers the operator
    with only "the provider returned an invalid response" while the server
    holds the exact, actionable cause. The names are the operator's own
    ``custom_inputs`` from step-2 field review (see
    ``guided_unproducible_output_field_names``), so returning them to that same
    operator discloses nothing new.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        detail_codes: tuple[str, ...] = (),
        unproducible_output_fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail_codes = detail_codes
        self.unproducible_output_fields = unproducible_output_fields


class PlannerDeclined(PipelinePlannerError):
    """Honest decline: the escape-hatch advisor answered in text, not a proposal.

    Raised only from the overtime turn. ``decline_text`` is the advisor's own
    explanation and is intended to be surfaced to the user as an ordinary
    assistant message, never as a provider failure.
    """

    def __init__(self, message: str, *, decline_text: str) -> None:
        super().__init__(message, code="DECLINED")
        self.decline_text = decline_text


@final
@dataclass(frozen=True, slots=True)
class GuidedPlannerDecline:
    """A ``PlannerDeclined`` outcome carried as a return value, not raised.

    Guided callers (``ComposerServiceImpl.plan_guided_full_pipeline`` and
    ``.plan_guided_pipeline``) catch ``PlannerDeclined`` themselves and
    return this instead of letting it propagate as a
    ``PipelinePlannerError``: a decline is a conversational outcome, not a
    planner failure, so it must never route through the guided operation's
    ``GuidedOperationFailureCode`` mapping. Callers persist ``decline_text``
    as an ordinary assistant chat message and complete the guided operation
    normally (mirrors the freeform surface's handling in
    ``ComposerServiceImpl.compose``).
    """

    decline_text: str

    def __post_init__(self) -> None:
        if type(self.decline_text) is not str:
            raise TypeError("GuidedPlannerDecline.decline_text must be an exact str")


class _PipelineCandidateRejected(RuntimeError):
    def __init__(self, result: ToolResult) -> None:
        super().__init__("pipeline candidate was not acceptable")
        self.result = result


@dataclass(frozen=True, slots=True)
class PlannerBudgetPolicy:
    """Request-wide hard bounds, except cost which is a continuation cap.

    ``max_request_bytes`` covers the exact canonical UTF-8 bytes of the full
    post-cache-marker ``{messages, tools}`` request payload.  Provider cost is
    necessarily known only after a call: the call is audited first, then a
    missing/malformed value or cumulative overage prevents all response
    parsing, dispatch, custody, and proposal construction.  The final call may
    therefore overshoot the configured amount; this is not a pre-spend cap.
    """

    max_total_provider_calls: int
    max_request_bytes: int
    max_completion_tokens: int
    max_cumulative_provider_cost: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("max_total_provider_calls", self.max_total_provider_calls),
            ("max_request_bytes", self.max_request_bytes),
            ("max_completion_tokens", self.max_completion_tokens),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact integer")
        if type(self.max_cumulative_provider_cost) is not Decimal:
            raise TypeError("max_cumulative_provider_cost must be Decimal")
        if not self.max_cumulative_provider_cost.is_finite() or self.max_cumulative_provider_cost < 0:
            raise ValueError("max_cumulative_provider_cost must be finite and non-negative")

    @property
    def audit_hash(self) -> str:
        return stable_hash(
            {
                "schema": "composer.planner-budget.v1",
                "max_total_provider_calls": self.max_total_provider_calls,
                "max_request_bytes": self.max_request_bytes,
                "max_completion_tokens": self.max_completion_tokens,
                "max_cumulative_provider_cost": str(self.max_cumulative_provider_cost),
            }
        )


@dataclass(frozen=True, slots=True)
class PlannerModelConfig:
    completion: _Completion
    model_identifier: str
    provider: str
    temperature: float | None
    seed: int | None
    timeout_seconds: float
    max_composition_turns: int
    max_discovery_turns: int
    max_tool_calls_per_turn: int
    max_api_attempts: int
    api_retry_base_seconds: float
    # Senior advisor model for the one-shot escape-hatch overtime turn.
    # None disables the hatch: budget exhaustion raises exactly as before.
    escape_hatch_model: str | None = None
    escape_hatch_provider: str | None = None
    # Endpoint affordance (Phase 3 Task 2): when the operator has pointed the
    # PRIMARY composer role at a custom OpenAI-compatible endpoint, these are
    # forwarded as ``api_base``/``api_key`` on every ordinary (non-hatch)
    # planner completion. None (the default) omits both kwargs entirely.
    # ``repr=False`` keeps the credential out of any dataclass repr that
    # might land in a log line or exception message.
    api_base: str | None = None
    api_key: str | None = field(default=None, repr=False)
    # Same affordance for the escape-hatch (ADVISOR) model — used only on
    # hatch turns, mirroring escape_hatch_model/escape_hatch_provider. Never
    # falls back to the primary api_base/api_key: the two roles are
    # deliberately independent (see composer_advisor_endpoint_base_url).
    escape_hatch_api_base: str | None = None
    escape_hatch_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for string_field_name, string_value in (
            ("model_identifier", self.model_identifier),
            ("provider", self.provider),
        ):
            if type(string_value) is not str or not string_value.strip():
                raise ValueError(f"{string_field_name} must be a non-empty exact string")
        if self.escape_hatch_model is not None and (type(self.escape_hatch_model) is not str or not self.escape_hatch_model.strip()):
            raise ValueError("escape_hatch_model must be a non-empty exact string or None")
        if self.escape_hatch_provider is not None and (
            type(self.escape_hatch_provider) is not str or not self.escape_hatch_provider.strip()
        ):
            raise ValueError("escape_hatch_provider must be a non-empty exact string or None")
        if (self.escape_hatch_model is None) != (self.escape_hatch_provider is None):
            raise ValueError("escape_hatch_model and escape_hatch_provider must be configured together")
        if self.escape_hatch_api_base is not None and self.escape_hatch_model is None:
            raise ValueError("escape_hatch_api_base requires escape_hatch_model to be configured")
        if self.escape_hatch_api_key is not None and self.escape_hatch_model is None:
            raise ValueError("escape_hatch_api_key requires escape_hatch_model to be configured")
        # Defense-in-depth pairing (belt-and-braces alongside
        # WebSettings._validate_composer_endpoint_credential_pairing): a
        # constructed PlannerModelConfig must never carry a base URL without
        # its explicit key, or vice versa, for either role. An endpoint with
        # no key would let LiteLLM silently fall back to an ambient provider
        # credential (e.g. OPENAI_API_KEY) and send it to the configured
        # endpoint. This does not replace the settings-level validator (the
        # only production source of these values); it forecloses the same
        # bug reappearing if a future caller ever constructs this dataclass
        # from something other than validated WebSettings fields.
        if (self.api_base is None) != (self.api_key is None):
            raise ValueError("api_base and api_key must be configured together (or both omitted)")
        if (self.escape_hatch_api_base is None) != (self.escape_hatch_api_key is None):
            raise ValueError("escape_hatch_api_base and escape_hatch_api_key must be configured together (or both omitted)")
        for integer_field_name, integer_value in (
            ("max_composition_turns", self.max_composition_turns),
            ("max_discovery_turns", self.max_discovery_turns),
            ("max_tool_calls_per_turn", self.max_tool_calls_per_turn),
            ("max_api_attempts", self.max_api_attempts),
        ):
            if type(integer_value) is not int or integer_value <= 0:
                raise ValueError(f"{integer_field_name} must be a positive exact integer")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int | float):
            raise TypeError("timeout_seconds must be a finite positive number")
        if not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        if isinstance(self.api_retry_base_seconds, bool) or not isinstance(self.api_retry_base_seconds, int | float):
            raise TypeError("api_retry_base_seconds must be a finite non-negative number")
        if not math.isfinite(float(self.api_retry_base_seconds)) or self.api_retry_base_seconds < 0:
            raise ValueError("api_retry_base_seconds must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class PlannerOriginatingMessage:
    session_id: str
    message_id: str | None
    content: str
    user_id: str | None

    def __post_init__(self) -> None:
        try:
            parsed = UUID(self.session_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("session_id must be a canonical UUID string") from exc
        if str(parsed) != self.session_id:
            raise ValueError("session_id must be a canonical UUID string")
        if self.message_id is not None:
            try:
                parsed_message_id = UUID(self.message_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("message_id must be a canonical UUID string or None") from exc
            if str(parsed_message_id) != self.message_id:
                raise ValueError("message_id must be a canonical UUID string or None")
        if type(self.content) is not str:
            raise TypeError("content must be an exact string")
        if self.user_id is not None and (type(self.user_id) is not str or not self.user_id.strip()):
            raise ValueError("user_id must be a non-empty exact string or None")


type PipelineCandidateFinalizer = Callable[[Mapping[str, Any]], Mapping[str, Any]]
type PipelineClaimEvaluator = Callable[[CompositionState, tuple[str, ...]], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class PlannerCustodyConfig:
    data_dir: str
    session_engine: Engine | None
    max_storage_per_session: int
    secret_service: WebSecretResolver | None
    runtime_preflight: RuntimePreflight | None
    write_fence: BlobGuidedOperationWriteFence | None = None

    def __post_init__(self) -> None:
        if type(self.data_dir) is not str or not self.data_dir.strip():
            raise ValueError("data_dir must be a non-empty exact string")
        if type(self.max_storage_per_session) is not int or self.max_storage_per_session <= 0:
            raise ValueError("max_storage_per_session must be a positive exact integer")
        if self.write_fence is not None and type(self.write_fence) is not BlobGuidedOperationWriteFence:
            raise TypeError("PlannerCustodyConfig.write_fence must be an exact BlobGuidedOperationWriteFence")


PlannerSettlement = Literal["complete", "failed", "cancelled"]
PipelineCustodyResult = Literal["not_required", "ready"]


slog = structlog.get_logger()


class _PlannerAttemptTrail:
    """Per-attempt planner observability: every round names its outcome.

    The terminal disposition (``composer.guided_planner_failure`` /
    ``planner_failure_disposition``) carries only the LAST failure's codes, so
    a run whose final attempt died at a non-candidate layer (shape, parse,
    deferred claim) reported ``rejection_codes=[]`` and the entire repair
    history was invisible. The trail emits one ``composer.planner_attempt``
    event per model response and one terminal ``composer.planner_summary`` on
    BOTH success and failure — the success summary is the churn-observability
    instrument (how many rounds a converging planner burned).

    Closed vocabularies only — same redaction discipline as the disposition
    logger: closed codes and kinds, identifiers, and counts; never raw
    provider text or row content.

    - ``phase``: discovery | candidate | repair | hatch | prose
    - ``outcome``: discovery_executed | candidate_rejected | arg_error |
      deferred_claim | truncated | prose_nudged | prose_reply |
      guard_fired | budget_exhausted | declined | accepted
    - ``led_to``: continue | repair | hatch | terminal | done
    - ``planner_code``: the closed loop-control code when a guard or budget
      resolved the attempt (e.g. DISCOVERY_CYCLE, REPAIR_EXHAUSTED)
    """

    def __init__(self, *, session_id: str, operation_id: str | None, surface: str) -> None:
        self.session_id = session_id
        self.operation_id = operation_id
        self.surface = surface
        self.attempts = 0
        self.phase_counts: dict[str, int] = {}
        self.rejection_history: list[dict[str, Any]] = []

    def begin_attempt(self) -> None:
        self.attempts += 1

    def log_attempt(
        self,
        phase: str,
        outcome: str,
        *,
        led_to: str,
        codes: tuple[str, ...] = (),
        planner_code: str | None = None,
        tool_calls: int = 0,
        repeated_fingerprint: bool = False,
    ) -> None:
        if phase not in self.phase_counts:
            self.phase_counts[phase] = 0
        self.phase_counts[phase] += 1
        rejection_codes = sorted(set(codes))
        if rejection_codes:
            self.rejection_history.append({"attempt": self.attempts, "outcome": outcome, "codes": rejection_codes})
        slog.info(
            "composer.planner_attempt",
            session_id=self.session_id,
            operation_id=self.operation_id,
            surface=self.surface,
            attempt=self.attempts,
            phase=phase,
            outcome=outcome,
            led_to=led_to,
            rejection_codes=rejection_codes,
            planner_code=planner_code,
            tool_calls=tool_calls,
            # True when this rejection's (component, code) fingerprint already
            # appeared in this request — the doctrine's "our bug" signal.
            repeated_fingerprint=repeated_fingerprint,
        )

    def log_summary(self, final_outcome: str) -> None:
        emit = slog.info if final_outcome == "accepted" else slog.warning
        emit(
            "composer.planner_summary",
            session_id=self.session_id,
            operation_id=self.operation_id,
            surface=self.surface,
            final_outcome=final_outcome,
            total_attempts=self.attempts,
            phase_counts=dict(self.phase_counts),
            rejection_history=list(self.rejection_history),
        )


@dataclass(frozen=True, slots=True)
class PipelinePlanResult:
    """Planner result carrying transport/custody facts outside the draft hash."""

    proposal: PipelineProposal
    tool_call_id: str
    custody_result: PipelineCustodyResult
    model_identifier: str
    model_version: str
    provider: str

    def __post_init__(self) -> None:
        if type(self.proposal) is not PipelineProposal:
            raise TypeError("proposal must be an exact PipelineProposal")
        if type(self.tool_call_id) is not str or not self.tool_call_id.strip():
            raise ValueError("tool_call_id must be a non-empty exact string")
        custody_result = cast(Any, self.custody_result)
        if type(custody_result) is not str or custody_result not in {"not_required", "ready"}:
            raise ValueError("custody_result must be 'not_required' or 'ready'")
        for name, value in (
            ("model_identifier", self.model_identifier),
            ("model_version", self.model_version),
            ("provider", self.provider),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a non-empty exact string")


@dataclass(frozen=True, slots=True)
class PlannerRequestLifecycle:
    """Injected route lifecycle adapters; the planner owns no route globals."""

    before_start: Callable[[], Awaitable[None]]
    request_scope: Callable[[], AbstractAsyncContextManager[None]]
    on_settled: Callable[[PlannerSettlement], Awaitable[None]]
    progress: ComposerProgressSink | None


@dataclass(frozen=True, slots=True)
class _ParsedToolCall:
    call_id: str
    name: str
    raw_arguments: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        freeze_fields(self, "arguments")


@dataclass(frozen=True, slots=True)
class _AuditedDiscoveryResult:
    """Carry the real result while exposing only a closed audit projection."""

    result: ToolResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.result.success,
            "validation": {"is_valid": self.result.validation.is_valid},
            "version": self.result.updated_state.version,
        }


class _PlannerTerminalPayload(BaseModel):
    """Typed runtime contract for the one planner terminal payload."""

    model_config = ConfigDict(extra="forbid", strict=True)

    pipeline: dict[str, Any]
    claimed_deferred_intent_ids: list[UUID] = Field(default_factory=list, json_schema_extra={"uniqueItems": True})

    @field_validator("claimed_deferred_intent_ids", mode="before")
    @classmethod
    def _require_canonical_unique_uuid_strings(cls, value: object) -> object:
        if type(value) is not list:
            raise ValueError("claimed_deferred_intent_ids must be an exact JSON array")
        canonical: list[str] = []
        for item in value:
            if type(item) is not str:
                raise ValueError("deferred intent claims must be canonical UUID strings")
            try:
                parsed = UUID(item)
            except ValueError as exc:
                raise ValueError("deferred intent claims must be canonical UUID strings") from exc
            if str(parsed) != item:
                raise ValueError("deferred intent claims must be canonical UUID strings")
            canonical.append(item)
        if len(set(canonical)) != len(canonical):
            raise ValueError("deferred intent claims must be unique")
        return [UUID(item) for item in canonical]


class _ClaimedDeferredIntentItemsSchema(TypedDict):
    type: str
    format: str


class _ClaimedDeferredIntentSchema(TypedDict):
    type: str
    items: _ClaimedDeferredIntentItemsSchema
    uniqueItems: bool


def _claimed_deferred_intent_schema() -> _ClaimedDeferredIntentSchema:
    schema = dict(_PlannerTerminalPayload.model_json_schema()["properties"]["claimed_deferred_intent_ids"])
    schema.pop("default", None)
    schema.pop("title", None)
    return cast(_ClaimedDeferredIntentSchema, schema)


def planner_terminal_tool_definition() -> dict[str, Any]:
    """Return the sole terminal with the exact registered pipeline schema."""
    return {
        "type": "function",
        "function": {
            "name": _TERMINAL_TOOL_NAME,
            "description": "Return one complete canonical pipeline proposal for server validation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline": canonical_set_pipeline_schema(),
                    "claimed_deferred_intent_ids": _claimed_deferred_intent_schema(),
                },
                "required": ["pipeline"],
                "additionalProperties": False,
            },
        },
    }


def planner_tool_definitions() -> list[dict[str, Any]]:
    """Return the pinned read-only palette followed by the sole terminal."""
    registered = {definition["name"]: definition for definition in get_tool_definitions()}
    missing = _PLANNER_DISCOVERY_TOOL_NAME_SET - registered.keys()
    if missing:
        raise RuntimeError(f"planner discovery declarations are missing: {sorted(missing)}")
    discovery = [
        {
            "type": "function",
            "function": {
                "name": registered[name]["name"],
                "description": registered[name]["description"],
                "parameters": registered[name]["parameters"],
            },
        }
        for name in PLANNER_DISCOVERY_TOOL_NAMES
    ]
    return [*discovery, planner_terminal_tool_definition()]


def _assert_planner_call_matches_manifest(
    call: ComposerLLMCall,
    manifest: PlannerCapabilityManifest,
    recorder: BufferingRecorder,
) -> None:
    """Audit the terminal provider outcome before rejecting input mutation.

    Matching calls continue to their normal single recording point.  A
    mismatch is itself a terminal integrity outcome, so the exact outbound
    hashes and provider status must be retained before the fail-closed error.
    """
    if call.messages_hash != manifest.rendered_prompt_hash or call.tools_spec_hash != manifest.effective_tool_hash:
        recorder.record_llm_call(call)
        raise AuditIntegrityError("planner call inputs changed after capability manifest construction")


def _provider_fields(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if value is None:
        return None
    try:
        fields = vars(value)
    except TypeError:
        return None
    try:
        extra = object.__getattribute__(value, "__pydantic_extra__")
    except AttributeError:
        return cast(Mapping[str, Any], fields)
    if isinstance(extra, Mapping) and extra:
        return cast(Mapping[str, Any], {**extra, **fields})
    return cast(Mapping[str, Any], fields)


def _provider_field(value: Any, name: str) -> Any:
    fields = _provider_fields(value)
    return fields[name] if fields is not None and name in fields else None


def _parse_json_object(raw: object, *, label: str) -> Mapping[str, Any]:
    if type(raw) is not str or not raw.strip():
        raise PipelinePlannerError(f"{label} must be a non-empty JSON string", code="MALFORMED_RESPONSE")
    try:
        parsed = bounded_json_loads(raw, label=label)
    except (json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError) as exc:
        raise PipelinePlannerError(f"{label} is not strict JSON", code="MALFORMED_RESPONSE") from exc
    if type(parsed) is not dict:
        raise PipelinePlannerError(f"{label} must decode to an object", code="MALFORMED_RESPONSE")
    return cast(Mapping[str, Any], parsed)


def _parse_response_tool_calls(
    response: Any,
    *,
    max_tool_calls: int,
    allow_text: bool = False,
) -> tuple[Any, tuple[_ParsedToolCall, ...]]:
    choices = _provider_field(response, "choices")
    if type(choices) not in {list, tuple} or len(choices) != 1:
        raise PipelinePlannerError("planner response must contain exactly one choice", code="MALFORMED_RESPONSE")
    message = _provider_field(choices[0], "message")
    if message is None:
        raise PipelinePlannerError("planner response choice is missing its message", code="MALFORMED_RESPONSE")
    raw_calls = _provider_field(message, "tool_calls")
    if type(raw_calls) not in {list, tuple} or not raw_calls:
        content = _provider_field(message, "content")
        if allow_text and type(content) is str and content.strip():
            try:
                require_bounded_text(content, label="planner text response")
            except JsonBoundaryError as exc:
                raise PipelinePlannerError("planner text response exceeds local bounds", code="MALFORMED_RESPONSE") from exc
            return message, ()
        # The no-tool-call class (prose thinking-aloud or an empty reply)
        # gets its own code so the loop can nudge-retry it — mid-plan prose
        # is ordinary LLM behaviour, not provider breakage. The loop
        # converts it back to terminal MALFORMED_RESPONSE once the nudge
        # budget is spent; the code never escapes the planner.
        raise PipelinePlannerError("planner response must call a declared tool", code="PROSE_REPLY")
    if len(raw_calls) > max_tool_calls:
        raise PipelinePlannerError("planner response exceeds the per-turn tool call limit", code="MALFORMED_RESPONSE")
    parsed: list[_ParsedToolCall] = []
    for raw_call in raw_calls:
        call_id = _provider_field(raw_call, "id")
        function = _provider_field(raw_call, "function")
        name = _provider_field(function, "name")
        raw_arguments = _provider_field(function, "arguments")
        if type(call_id) is not str or not call_id or type(name) is not str or not name:
            raise PipelinePlannerError("planner tool call metadata is malformed", code="MALFORMED_RESPONSE")
        arguments = _parse_json_object(raw_arguments, label=f"{name} arguments")
        parsed.append(_ParsedToolCall(call_id, name, cast(str, raw_arguments), arguments))
    terminal_calls = tuple(call for call in parsed if call.name == _TERMINAL_TOOL_NAME)
    if terminal_calls and len(parsed) != 1:
        raise PipelinePlannerError("terminal proposal call must be the only tool call", code="MALFORMED_RESPONSE")
    return message, tuple(parsed)


def _discovery_pressure_notice(remaining: int) -> str:
    return (
        f"Budget notice: only {remaining} discovery turns remain before this planning request is cut off. "
        "Stop exploring, settle the design, and call emit_pipeline_proposal with one complete proposal "
        "as soon as possible."
    )


def _truncated_response_notice() -> str:
    return (
        "Your previous response was cut off at the output token limit and has been discarded. "
        "Respond again more compactly: shorter prompt templates, omit optional fields, and emit "
        "the tool call with no surrounding prose."
    )


# Bounded retries for the no-tool-call response class, separate from the
# repair budget: repairs answer candidate rejections, nudges answer a model
# that thought aloud instead of calling a tool (tutorial session a2513c3c
# died terminal on a single prose reply with its whole repair budget unspent).
_PROSE_NUDGE_BUDGET = 2

# Message prefix every ``bind_guided_reviewed_components`` complaint about the
# SHAPE OF THE CANDIDATE carries (guided/planning.py). Those describe what the
# planner authored, so they are repairable; every other AuditIntegrityError
# reaching the finalizer describes server-side authority and stays terminal.
_CANDIDATE_SHAPE_INTEGRITY_PREFIX: Final[str] = "guided planner candidate"


def _prose_reply_notice() -> str:
    return "Your previous reply called no tool. You must respond with a declared tool call — continue from where you were."


def _escape_hatch_notice() -> str:
    return (
        "The planning budget is exhausted; this is the escape hatch. You are a senior advisor model "
        "seeing the full conversation above as one freeform puzzle. You have exactly one turn: either "
        "call emit_pipeline_proposal once with a complete, valid pipeline that satisfies the request, "
        "or reply in plain text honestly explaining why the request cannot be built with the available "
        "plugins. Do not call any other tool. If you decline, your FIRST sentence must state the cause "
        "plainly in the user's terms — distinguish a capability that is installed but not turned on in "
        "this deployment (an operator can enable it) from one that does not exist or a request that is "
        "impossible in principle. For example: \"I can't do this here: the LLM transform is not turned "
        'on in this deployment — an operator needs to enable an LLM profile." Put supporting detail '
        "after that sentence, not before it."
    )


def _assistant_tool_calls_message(message: Any, calls: tuple[_ParsedToolCall, ...]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": _provider_field(message, "content"),
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.raw_arguments},
            }
            for call in calls
        ],
    }


def _feedback_error_codes(feedback: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract the closed error codes from a structural feedback envelope."""
    validation = cast(Mapping[str, Any], feedback["validation"])
    errors = cast(list[Mapping[str, Any]], validation["errors"])
    return tuple(cast(str, entry["error_code"]) for entry in errors)


def _transform_node_count(pipeline: Mapping[str, Any]) -> int:
    """Count transform/aggregation nodes in a planner-authored pipeline dict."""
    nodes = cast(list[Mapping[str, Any]], pipeline["nodes"])
    return sum(1 for node in nodes if node["node_type"] in ("transform", "aggregation"))


# A stated routing threshold: a comparison operator, or comparison wording,
# bound to a NUMBER. Requiring the number is what keeps this conservative —
# "the transform above the gate" and "source -> sink" are ordinary authoring
# prose and must never trip the guard. The operator lookbehind rejects arrow
# and fat-arrow forms outright.
_THRESHOLD_NUMBER: Final[str] = r"\$?\d+(?:[.,]\d+)*(?!\d)"
# A number immediately followed by a unit noun measures a LIMIT — prompt
# length, a row cap, a branch count — not a value a row is routed on. The
# trailing ``(?!\d)`` on the number above is load-bearing: without it the
# engine backtracks to a shorter number ("5" out of "50 words") and slips past
# this lookahead.
_THRESHOLD_UNIT_NOUN: Final[str] = (
    r"(?!\s*(?:%|(?:words?|characters?|chars?|rows?|records?|branch|branches|sinks?|nodes?|tokens?|"
    r"seconds?|secs?|minutes?|ms|milliseconds?|times?|items?|entries|columns?|fields?)\b))"
)
_THRESHOLD_QUANTITY: Final[str] = _THRESHOLD_NUMBER + _THRESHOLD_UNIT_NOUN
_THRESHOLD_OPERATOR: Final[str] = r"(?<![-=<>!])(?:>=|<=|==|>|<)"
_THRESHOLD_WORDING: Final[str] = (
    r"(?:greater than|less than|more than|fewer than|at least|at most|no more than|no less than|above|below|over|under)"
)
_STATED_THRESHOLD_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z_]\w*\s*{_THRESHOLD_OPERATOR}\s*{_THRESHOLD_QUANTITY}"
    rf"|{_THRESHOLD_NUMBER}\s*{_THRESHOLD_OPERATOR}\s*[A-Za-z_]\w*"
    rf"|{_THRESHOLD_WORDING}\s+{_THRESHOLD_QUANTITY}",
    re.IGNORECASE,
)


# Routing intent. A comparison alone is not enough: "summarise each row in
# under 50 words" and "keep at most 100 rows" bind comparison wording to a
# number while asking for nothing about routing, and firing on those would
# tell the model to author a gate condition a correct pipeline never needed —
# turning a right answer into a wrong one. Both halves must hold.
_ROUTING_INTENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:route|routes|routed|routing|send|sends|sent|go to|goes to|split|splits|gate|divert|diverts|separate|separates)\b",
    re.IGNORECASE,
)
# Clause boundaries. "Split the rows into two sinks AND keep at most 100 rows"
# states a routing action and an unrelated cap; only a comparison in the SAME
# clause as the routing verb is plausibly the rule that routes.
_CLAUSE_BOUNDARY_PATTERN: Final[re.Pattern[str]] = re.compile(
    # ``\.(?!\d)`` splits sentences without splitting decimals: a decimal
    # point is always followed by a digit, a full stop never is.
    r"\.(?!\d)|[;:!?\n]|\band\b|\bthen\b|\bbut\b|\bwhile\b|\balso\b",
    re.IGNORECASE,
)


def _stated_threshold_in(instruction: str) -> str | None:
    """Return the routing comparison the instruction states, or None.

    Deliberately conservative on three axes, because a false positive here is
    worse than a miss: the rejection asserts the instruction stated a routing
    rule and tells the model to author a gate condition, so on a pipeline that
    is already correct a compliant model makes it wrong — and under a repair
    budget of one that ends in REPAIR_EXHAUSTED with no proposal at all. A
    comparison counts only when

    1. an operator (or comparison wording) is bound to a literal number;
    2. the number is NOT followed by a unit noun (``under 50 words``,
       ``at most 100 rows``, ``more than 2 branches`` measure a limit, not a
       row value); and
    3. it shares a clause with a routing verb.

    Known false negative, accepted: the expression form
    ``row['amount'] > 500`` is missed, because the ``]`` breaks the
    ``[A-Za-z_]\\w*`` operand. Widening the operand to swallow bracket
    subscripts would also swallow ordinary prose ahead of an operator, and
    false negatives are the safe direction — the non-blocking
    ``gate_fan_out_advisory`` still names the shape at review.
    """
    for clause in _CLAUSE_BOUNDARY_PATTERN.split(instruction):
        if _ROUTING_INTENT_PATTERN.search(clause) is None:
            continue
        match = _STATED_THRESHOLD_PATTERN.search(clause)
        if match is not None:
            return match.group(0).strip()
    return None


def _threshold_homeless_gate_id(pipeline: Mapping[str, Any]) -> str | None:
    """Return a constant-condition gate id when NO gate carries a real condition.

    Both halves matter. A candidate that authored a row-reading condition
    somewhere has a home for the stated comparison and is left alone, even if
    it also contains a constant fan-out gate; only a candidate whose every
    gate is constant has provably dropped the rule.
    """
    gates = [node for node in cast(list[Mapping[str, Any]], pipeline["nodes"]) if node["node_type"] == "gate"]
    constant_gate_ids = [
        str(gate["id"]) for gate in gates if isinstance(gate.get("condition"), str) and gate_condition_is_constant(gate["condition"])
    ]
    if not constant_gate_ids or len(constant_gate_ids) != len(gates):
        return None
    return constant_gate_ids[0]


def _stated_threshold_ignored_rejection(state: CompositionState, *, node_id: str, stated: str) -> ToolResult:
    """Synthesize the coded rejection for a dropped routing threshold.

    AWS acceptance run 2 (R2-F17, elspeth-5c0c09db31): asked to route rows on
    ``amount > 500``, the planner authored a constant-condition gate forking
    every row to BOTH sinks. That shape is legal — it is the documented
    fan-out macro, generated by our own authoring aids — so it can never be a
    structural rejection. What makes it wrong HERE is the instruction, which
    only the planner loop can see.

    The message quotes the comparison span from the instruction the planner's
    own prompt was built from — content already verbatim in its context, the
    same custody judgment ``plugin_options_invalid`` detail rides on — because
    a bare code cannot tell the model which rule it dropped.
    """
    entry = ValidationEntry(
        component=f"node:{node_id}",
        message=(
            f"Gate '{node_id}' has a constant condition, so it makes no per-row decision, but the "
            f'instruction states the comparison "{stated}". Every row would take the same path.'
        ),
        severity="high",
        error_code="gate_condition_ignores_stated_threshold",
    )
    return ToolResult(
        success=False,
        updated_state=state,
        validation=ValidationSummary(is_valid=False, errors=(entry,), warnings=(), suggestions=()),
        affected_nodes=(node_id,),
    )


_UNPRODUCIBLE_OUTPUT_FIELDS_CODE: Final[str] = "passthrough_cannot_produce_declared_fields"


def _unproducible_output_fields_rejection(state: CompositionState, *, fields: tuple[str, ...]) -> ToolResult:
    """Synthesize the coded rejection for a zero-transform candidate with a gap.

    R2-F4 (elspeth-6e311df389). Step-2 field review let the operator declare
    output fields no reviewed source declares or observes; a candidate with no
    transform or aggregation node has nothing that could produce them, so it is
    unbuildable no matter how it is wired. Structural validation cannot answer
    it: the sink-contract check emits no contract at all when the source
    abstains from propagation (ADR-007), which is exactly the observed-schema
    case. Only the planner loop sees both the reviewed gap and the candidate's
    node count, so the rejection lives here.

    Deliberately NOT one-shot with an omit-valve, unlike the nodeless-revision
    and stated-threshold nudges: those infer intent from PROSE ELSPETH cannot
    prove, so re-emitting is a legitimate "I meant it". This is a mechanical set
    difference over reviewed facts, and adding ANY transform clears the guard in
    one turn — so it fires on every attempt, including the escape hatch, rather
    than letting the second identical candidate through. Repeated identical
    rejections draw the ordinary repeat notice via the shared fingerprint path.

    The message names the missing fields. They are the operator's own
    ``custom_inputs`` strings, already verbatim in the planner's
    ``reviewed_planner_context`` (``outputs[].required_fields``) — the same
    custody judgment ``gate_condition_ignores_stated_threshold`` rides on.
    """
    entry = ValidationEntry(
        component="pipeline",
        message=(
            "This candidate has no transform or aggregation nodes, so it can only emit what the source "
            f"carries, but no reviewed source declares or observes these reviewed output fields: {', '.join(fields)}."
        ),
        severity="high",
        error_code=_UNPRODUCIBLE_OUTPUT_FIELDS_CODE,
    )
    return ToolResult(
        success=False,
        updated_state=state,
        validation=ValidationSummary(is_valid=False, errors=(entry,), warnings=(), suggestions=()),
        affected_nodes=(),
    )


def _nodeless_revision_rejection(state: CompositionState) -> ToolResult:
    """Synthesize the coded rejection for a nodeless revision candidate.

    The entry rides the normal candidate-rejection path so repair budget,
    hatch, and feedback projection all apply uniformly; the static guidance
    lives in the closed catalogue under the code (tools.generation).
    """
    entry = ValidationEntry(
        component="pipeline",
        message=(
            "Revision candidate contains no transform or aggregation nodes; "
            "the revision instruction asked for processing this pipeline does not perform."
        ),
        severity="high",
        error_code="proposal_missing_requested_transforms",
    )
    return ToolResult(
        success=False,
        updated_state=state,
        validation=ValidationSummary(is_valid=False, errors=(entry,), warnings=(), suggestions=()),
        affected_nodes=(),
    )


def _missing_source_rejection(state: CompositionState) -> ToolResult:
    """Synthesize the coded rejection for a candidate that names no source.

    Both ``source`` and ``sources`` are optional on the terminal schema, so a
    re-plan "delta" candidate that drops the source block is schema-legal and
    reaches the candidate finalizer. The guided finalizer binds reviewed
    component authority and has nothing to bind, so it answers that shape with
    ``AuditIntegrityError`` — a terminal 500 for what is an ordinary authoring
    slip (elspeth-bcc6bdac99). Rejecting the shape here, ahead of any
    finalizer, keeps the repair identical on every surface: the same
    ``no_source_configured`` entry ``set_pipeline`` already produces, carrying
    the catalogue's "include a source block" fix.
    """
    entry = ValidationEntry(
        component="rejected_mutation",
        message="set_pipeline requires source or sources.",
        severity="high",
        error_code="no_source_configured",
    )
    return ToolResult(
        success=False,
        updated_state=state,
        validation=ValidationSummary(is_valid=False, errors=(entry,), warnings=(), suggestions=()),
        affected_nodes=(),
    )


def _rejection_entries(result: ToolResult) -> tuple[Any, ...]:
    """Return the entries the planner should actually repair against.

    A pre-application semantic rejection (``_failure_result``) prepends a
    ``rejected_mutation`` entry naming the real reason and then appends
    ``state.validate()`` of the UNCHANGED current state. On the guided and
    tutorial surfaces that current state is the empty seed, so every such
    rejection also carried ``no_source_configured`` + ``no_sinks_configured``
    — red herrings describing a state the planner is not editing (set_pipeline
    authors a full replacement). Tutorial session 38e3e7f8 (op 1152d7e3,
    2026-07-22) burned its repair budget on exactly that noise and "converged"
    by dropping every node. When rejection entries are present, they are the
    ONLY entries feedback and trail may carry; validated-candidate rejections
    (no ``rejected_mutation`` entry) pass through untouched.
    """
    rejection = tuple(entry for entry in result.validation.errors if entry.component == "rejected_mutation")
    return rejection if rejection else tuple(result.validation.errors)


def _candidate_rejection_codes(result: ToolResult) -> tuple[str, ...]:
    """Name every rejection entry for the per-attempt trail, coded or not.

    A codeless entry surfaces as the ``"validation_error"`` placeholder —
    the same fallback ``_allowlisted_candidate_feedback`` projects — rather
    than silently vanishing. Filtering codeless entries out produced
    REPAIR_EXHAUSTED trails with ``rejection_codes=[]`` while rejections
    existed (guided session 5113b7ac, 2026-07-22): the run looked
    rejection-free precisely when the planner was blindest.
    """
    return tuple(entry.error_code or "validation_error" for entry in _rejection_entries(result))


# Closed routing-destination codes whose feedback carries instance wiring
# facts from ``route_destination_facts`` (the rejected candidate's dangling
# value plus its valid destinations). Same custody class as the coalesce
# reachability facts: node ids, sink names, and connection names the planner
# itself authored in the candidate being answered.
_ROUTE_DESTINATION_FACT_CODES: Final[frozenset[str]] = frozenset(
    {
        "source_on_success_dangling",
        "transform_on_success_dangling",
        "aggregation_on_success_dangling",
        "transform_on_error_unknown_sink",
        "coalesce_on_success_unknown_sink",
    }
)


def _rejection_fingerprint(result: ToolResult) -> tuple[tuple[str, str], ...]:
    """Identity of one candidate rejection: sorted (component, code) pairs.

    Two rejections with the same fingerprint failed for the same reasons on
    the same components — the previous repair changed nothing that mattered.
    Project doctrine: an identical fingerprint repeating across attempts is a
    feedback-quality defect (ours), so the loop must at minimum TELL the model
    the repetition happened instead of silently burning budget on it.
    """
    return tuple(sorted((entry.component, entry.error_code or "validation_error") for entry in _rejection_entries(result)))


_REPEAT_NOTICE = (
    "This candidate failed with EXACTLY the same rejection set (same components, same codes) "
    "as an earlier candidate in this request: the intervening changes did not fix — or "
    "reintroduced — the failure. Change ONLY the fields the errors below name, keep every "
    "other part of your last candidate byte-identical, and re-emit."
)


def _allowlisted_candidate_feedback(result: ToolResult, *, repeated_fingerprint: bool = False) -> dict[str, Any]:
    """Project only structured validation fields already safe for tool output.

    Raw validation messages are withheld — they can quote plugin names, option
    values, or row content. Each closed ``error_code`` is enriched with the
    static ``(explanation, suggested_fix)`` the ``explain_validation_error``
    tool would return (the single source of truth lives in
    ``tools.generation``), so the repair turn carries the fix a bare code
    cannot — e.g. "there is no 'fork' node_type; fork with a gate", or the
    registered pipeline-decision kinds. The enrichment text is a public
    constant, never per-request data, so it does not re-open the message
    boundary this allowlist protects. Codes with no catalogue entry stay bare.

    ``plugin_options_invalid`` additionally carries the validator's own
    message as ``detail``. That message quotes only the OPTIONS OF THE
    REJECTED CANDIDATE — content the planner itself authored in the very
    tool call being answered, already present verbatim in its context and
    in the session's tool audit — so echoing it back to the same planner
    crosses no custody boundary. Withholding it made an exactly-repairable
    rejection unrepairable: on run 06c9ec49 (2026-07-29) the validator
    named the missing ``required_input_fields`` declaration and its
    one-line patch, the planner never saw either, burned every repair on
    the static enrichment's profile-alias hypothesis, and declined with a
    confabulated cause — twice, in two sessions.
    """
    validation = result.validation
    errors: list[dict[str, Any]] = []
    reachability_facts: dict[str, dict[str, Any]] | None = None
    destination_facts: dict[str, RouteDestinationFactDict] | None = None
    for entry in _rejection_entries(result):
        code = entry.error_code or "validation_error"
        projected: dict[str, Any] = {
            "component": entry.component,
            "severity": entry.severity,
            "error_code": code,
            "error_class": "ValidationError",
        }
        guidance = explain_validation_code(code)
        if guidance is not None:
            projected["explanation"], projected["suggested_fix"] = guidance
        if code in ("plugin_options_invalid", "gate_condition_ignores_stated_threshold", _UNPRODUCIBLE_OUTPUT_FIELDS_CODE):
            # Same custody judgment for all three: the message quotes only
            # content the planner itself already holds verbatim — the options
            # of the candidate it just authored, the comparison span from the
            # instruction its own prompt was built from, or the reviewed output
            # field names already in its ``reviewed_planner_context``.
            projected["detail"] = entry.message
        if code in _ROUTE_DESTINATION_FACT_CODES:
            # Instance wiring facts derived from the REJECTED candidate state
            # the result carries — the dangling value and the exact valid
            # destinations (sink names / consumable connections) the planner
            # itself authored. Without them the bare code names neither WHICH
            # value dangled nor what it should match, and the static guidance
            # can only send the model to get_pipeline_state — which reads the
            # BASELINE session state, not the rejected candidate (empty on a
            # fresh compose). AWS acceptance runs 2026-07-30 exhausted their
            # repair budget on exactly that blindness (elspeth-5904b1683a).
            if destination_facts is None:
                destination_facts = route_destination_facts(result.updated_state)
            projected["connectivity"] = destination_facts[entry.component]
        if code == "coalesce_branch_unreachable":
            # Instance wiring facts derived from the REJECTED state the result
            # carries — same redaction class as the contract facts below (node
            # ids + connection names the planner itself authored). Without
            # them the observed miswiring (branch transforms publishing past
            # the coalesce, e.g. straight to a sink) is invisible: guided
            # session 277fb6c4 burned its whole repair budget re-emitting the
            # coalesce because nothing named the connections that actually
            # exist.
            if reachability_facts is None:
                reachability_facts = coalesce_reachability_facts(result.updated_state)
            projected["connectivity"] = reachability_facts[entry.component.removeprefix("node:")]
        if entry.contract is not None:
            # Structured contract facts: producer/consumer component ids and
            # schema FIELD NAMES from validated contract config — pipeline
            # metadata the session owner authored, never user row content
            # (see SchemaContractDetail in composer.state). Without them a
            # schema-contract rejection is a bare code the planner cannot
            # repair within budget: it must know WHICH edge failed and WHICH
            # fields are missing. This stays inside the message-redaction
            # boundary this allowlist protects.
            projected["contract"] = entry.contract.to_dict()
        if entry.row_union_schema is not None:
            # Structured row-union branch declarations: branch aliases,
            # schema modes, field names, and declared field properties from
            # the REJECTED candidate the planner authored. These are the
            # row-union equivalent of the safe contract facts above, never
            # runtime row content, and make the incompatibility repairable
            # without exposing the free-form validation message.
            projected["row_union_schema"] = entry.row_union_schema.to_dict()
        errors.append(projected)
    feedback: dict[str, Any] = {
        "success": False,
        "validation": {
            "is_valid": validation.is_valid,
            "errors": errors,
        },
        # Static usage line, never per-request data. Live planners called
        # explain_validation_error with junk ({"error_text": "ValidationError"})
        # because nothing said the exact code string is the lookup key. Kept
        # deliberately free of topology hints — mid-repair suggestions have
        # derailed otherwise-converging repairs.
        "guidance": "To expand any code, call explain_validation_error with the exact code string.",
    }
    if repeated_fingerprint:
        # Static text, never per-request data: names the repetition the model
        # cannot see on its own (it has no attempt counter) so budget stops
        # burning on byte-identical failures without the model knowing.
        feedback["repeat_notice"] = _REPEAT_NOTICE
    return feedback


class _DeferredIntentClaimFeedbackError(TypedDict):
    component: str
    severity: str
    error_class: str
    error_code: str


class _DeferredIntentClaimFeedbackValidation(TypedDict):
    is_valid: bool
    errors: list[_DeferredIntentClaimFeedbackError]


class _DeferredIntentClaimFeedback(TypedDict):
    success: bool
    validation: _DeferredIntentClaimFeedbackValidation


def _deferred_intent_claim_feedback() -> _DeferredIntentClaimFeedback:
    return {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [
                {
                    "component": "claimed_deferred_intent_ids",
                    "severity": "high",
                    "error_class": "DeferredIntentClaimError",
                    "error_code": "deferred_intent_claim",
                }
            ],
        },
    }


def _canonical_schema_feedback() -> dict[str, Any]:
    return {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [
                {
                    "component": "pipeline",
                    "severity": "high",
                    "error_code": "canonical_schema",
                    "error_class": "SchemaValidationError",
                }
            ],
        },
    }


def _allowlisted_argument_feedback(error: ToolArgumentError) -> Mapping[str, Any]:
    """Project a semantic argument failure without its message or input."""
    return {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [
                {
                    "component": error.argument,
                    "severity": "high",
                    "error_code": error.code or "argument_error",
                    "error_class": "ToolArgumentError",
                }
            ],
        },
    }


class _ClosedProviderValidationEntry(TypedDict):
    component: str
    severity: str
    error_code: str


class _ClosedProviderValidationEnvelope(TypedDict):
    is_valid: bool
    errors: list[_ClosedProviderValidationEntry]
    warnings: list[_ClosedProviderValidationEntry]
    suggestions: list[_ClosedProviderValidationEntry]
    semantic_contracts: list[object]
    graph_repair_suggestions: list[object]


class _ClosedProviderDiscoveryPayload(TypedDict):
    success: bool
    validation: _ClosedProviderValidationEnvelope
    affected_nodes: list[str]
    version: int
    data: NotRequired[object]


def _closed_provider_validation_entry(
    entry: ValidationEntry,
    *,
    fallback_code: str,
) -> _ClosedProviderValidationEntry:
    """Project one validation entry without state-bearing text or attribution."""
    return {
        "component": "pipeline",
        "severity": entry.severity,
        "error_code": entry.error_code or fallback_code,
    }


def _closed_provider_discovery_payload(result: ToolResult) -> _ClosedProviderDiscoveryPayload:
    """Return the closed ToolResult envelope allowed on restricted surfaces.

    Validation messages, semantic contracts, graph repair arguments, runtime
    preflight details, and optional augmentation fields can all contain
    authoritative option values. The restricted provider needs only validity,
    closed codes and severities, stable pipeline attribution, outcome, version,
    and the separately projected discovery data.
    """
    validation = result.validation
    payload: _ClosedProviderDiscoveryPayload = {
        "success": result.success,
        "validation": {
            "is_valid": validation.is_valid,
            "errors": [_closed_provider_validation_entry(entry, fallback_code="validation_error") for entry in validation.errors],
            "warnings": [_closed_provider_validation_entry(entry, fallback_code="validation_warning") for entry in validation.warnings],
            "suggestions": [
                _closed_provider_validation_entry(entry, fallback_code="validation_suggestion") for entry in validation.suggestions
            ],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": list(result.affected_nodes),
        "version": result.updated_state.version,
    }
    if result.data is not None:
        payload["data"] = deep_thaw(result.data)
    return payload


def _serialize_closed_provider_discovery_payload(payload: Mapping[str, Any]) -> str:
    """Serialize a closed provider payload with canonical ToolResult support."""
    return json.dumps(payload, default=pydantic_default)


def _serialize_provider_discovery_result(
    *,
    call: _ParsedToolCall,
    result: ToolResult,
    surface: PlannerSurface,
    provider_current_state: Mapping[str, Any],
) -> str:
    """Serialize one discovery result through the planner surface disclosure.

    Staged/tutorial callers already supply their policy-owned
    ``provider_current_state`` projection for the initial request. Reuse that
    exact projection for later state reads and close the validation envelope
    on every discovery result. Discovery execution, audit, validation, and
    candidate construction continue to use the authoritative
    ``CompositionState``. Non-state discovery retains its canonical outcome
    and data; preview fails closed because its data duplicates authoritative
    validation, runtime preflight, and proof diagnostics. Failed reads retain
    their canonical outcome and leak-safe error data. Successful state
    component reads follow the authoritative result shape, so node/output
    identifiers that collide with full-state aliases keep dispatch precedence.
    """
    if surface not in {PlannerSurface.GUIDED_STAGED, PlannerSurface.TUTORIAL_PROFILE}:
        return serialize_tool_result(result)
    payload = _closed_provider_discovery_payload(result)

    def fail_closed() -> None:
        payload["success"] = False
        payload["data"] = {
            "error": "The requested component is unavailable on this planner disclosure surface.",
            "error_code": "surface_projection_unavailable",
        }

    if call.name == "preview_pipeline":
        if result.success:
            fail_closed()
        return _serialize_closed_provider_discovery_payload(payload)
    if call.name != "get_pipeline_state":
        return _serialize_closed_provider_discovery_payload(payload)
    if not result.success:
        return _serialize_closed_provider_discovery_payload(payload)

    authoritative_data = result.data
    component = call.arguments.get("component")
    if component == "set_pipeline_arguments":
        fail_closed()
    elif isinstance(authoritative_data, Mapping) and set(authoritative_data) == {"sources"}:
        payload["data"] = {"sources": deep_thaw(provider_current_state.get("sources", []))}
    elif isinstance(authoritative_data, Mapping) and set(authoritative_data) == {"node"}:
        selected = authoritative_data["node"]
        nodes = provider_current_state.get("nodes", [])
        selected_id = selected.get("id") if isinstance(selected, Mapping) else None
        node = next(
            (candidate for candidate in nodes if isinstance(candidate, Mapping) and candidate.get("id") == selected_id),
            None,
        )
        if node is not None:
            payload["data"] = {"node": deep_thaw(node)}
        else:
            fail_closed()
    elif isinstance(authoritative_data, Mapping) and set(authoritative_data) == {"output"}:
        selected = authoritative_data["output"]
        outputs = provider_current_state.get("outputs", [])
        selected_name = selected.get("sink_name") if isinstance(selected, Mapping) else None
        output = next(
            (candidate for candidate in outputs if isinstance(candidate, Mapping) and candidate.get("name") == selected_name),
            None,
        )
        if output is not None:
            payload["data"] = {"output": deep_thaw(output)}
        else:
            fail_closed()
    elif isinstance(authoritative_data, Mapping) and "inspection" in authoritative_data:
        payload["data"] = deep_thaw(provider_current_state)
    else:
        fail_closed()
    return _serialize_closed_provider_discovery_payload(payload)


async def _await_custody_settlement(awaitable: Awaitable[Any]) -> Any:
    """Finish idempotent custody after cancellation, then preserve cancellation."""

    async def settle() -> Any:
        return await awaitable

    task: asyncio.Task[Any] = asyncio.create_task(settle())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        # Observe a custody failure without replacing the active cancellation.
        with suppress(BaseException):
            task.result()
        raise


async def _settle_lifecycle(lifecycle: PlannerRequestLifecycle, outcome: PlannerSettlement) -> None:
    async def settle() -> None:
        await lifecycle.on_settled(outcome)

    task: asyncio.Task[None] = asyncio.create_task(settle())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Settlement is operator/UI bookkeeping.  Let it finish even while the
        # request task is being torn down, observe its result, then preserve
        # the original cancel. A second cancellation cannot turn a settlement
        # failure into an unobserved task exception.
        while not task.done():
            with suppress(BaseException):
                await asyncio.shield(task)
        with suppress(BaseException):
            task.result()
        raise


async def _build_valid_pipeline_plan(
    *,
    pipeline: Mapping[str, Any],
    current_state: CompositionState,
    base: ProposalBase,
    reviewed_facts: Mapping[str, Any],
    claimed_deferred_intent_ids: tuple[str, ...],
    claim_evaluator: PipelineClaimEvaluator | None,
    supersedes_draft_hash: str | None,
    surface: PlannerSurface,
    repair_count: int,
    skill_hash: str,
    tool_call_id: str,
    terminal_context: ToolContext,
    custody_config: PlannerCustodyConfig,
    originating_message: PlannerOriginatingMessage,
    run_sync: Callable[..., Awaitable[Any]],
    model_identifier: str,
    model_version: str,
    provider: str,
) -> PipelinePlanResult:
    """Validate, settle custody, revalidate, and seal one exact pipeline."""

    # Validate the exact provider-authored payload before adding server-owned
    # interpretation identity/status. Otherwise a forged canonical-looking row
    # would be indistinguishable from the trusted canonicalizer's output.
    candidate_context = replace(
        terminal_context,
        tool_arguments_hash=stable_hash({"pipeline": project_composer_authority_payload(pipeline)}),
    )
    try:
        candidate = await run_sync(
            build_set_pipeline_candidate,
            pipeline,
            current_state,
            candidate_context,
        )
    except (KeyError, TypeError, ValueError) as exc:
        # An unguarded lookup escaping the candidate builder (e.g. a review-row
        # field an interpretation-state walk subscripts without a guard) is a
        # server defect, not a recoverable candidate rejection. Convert it to
        # the planner's typed failure idiom naming the offending key so the
        # route records a leak-safe disposition instead of a raw 500.
        raise PipelinePlannerError(
            f"pipeline candidate construction raised an unguarded {type(exc).__name__} ({exc})",
            code="CANDIDATE_CONSTRUCTION_ERROR",
        ) from exc
    if not candidate.acceptable:
        raise _PipelineCandidateRejected(candidate.result)

    # Seal and hash the canonical server-owned representation only after the
    # raw authoring boundary accepted the compact review shells.
    pipeline = canonicalize_authored_node_review_requirements(pipeline, current_state=current_state)
    candidate_context = replace(
        terminal_context,
        tool_arguments_hash=stable_hash({"pipeline": project_composer_authority_payload(pipeline)}),
        _interpretation_requirements_are_internal=True,
    )
    candidate = await run_sync(
        build_set_pipeline_candidate,
        pipeline,
        current_state,
        candidate_context,
    )
    if not candidate.acceptable:
        raise AuditIntegrityError("canonical interpretation requirements failed candidate revalidation")
    covered_deferred_intent_ids = (
        claim_evaluator(candidate.result.updated_state, claimed_deferred_intent_ids)
        if claimed_deferred_intent_ids and claim_evaluator is not None
        else ()
    )
    if claimed_deferred_intent_ids and claim_evaluator is None:
        raise DeferredIntentClaimError("this planner surface has no eligible deferred intent claims")
    if type(covered_deferred_intent_ids) is not tuple or any(type(intent_id) is not str for intent_id in covered_deferred_intent_ids):
        raise AuditIntegrityError("deferred intent claim evaluator returned malformed coverage")
    if len(set(covered_deferred_intent_ids)) != len(covered_deferred_intent_ids) or set(covered_deferred_intent_ids) != set(
        claimed_deferred_intent_ids
    ):
        raise AuditIntegrityError("deferred intent claim evaluator changed the claimed identity set")

    safe_pipeline: Mapping[str, Any] = pipeline
    custody_result: PipelineCustodyResult = "not_required"
    if candidate.prepared_inline_blob is not None:
        if custody_config.session_engine is None:
            raise AuditIntegrityError("inline pipeline custody requires session_engine")
        preparation = prepare_pipeline_custody(
            pipeline,
            candidate.prepared_inline_blob,
            session_id=originating_message.session_id,
        )
        await _await_custody_settlement(
            finalize_pipeline_custody(
                preparation,
                engine=custody_config.session_engine,
                data_dir=custody_config.data_dir,
                max_storage_per_session=custody_config.max_storage_per_session,
                write_fence=custody_config.write_fence,
            )
        )
        safe_pipeline = cast(dict[str, Any], deep_thaw(preparation.arguments))
        safe_context = replace(
            terminal_context,
            tool_arguments_hash=stable_hash({"pipeline": project_composer_authority_payload(safe_pipeline)}),
            _interpretation_requirements_are_internal=True,
        )
        safe_candidate = await run_sync(
            build_set_pipeline_candidate,
            safe_pipeline,
            current_state,
            safe_context,
        )
        if not safe_candidate.acceptable or safe_candidate.prepared_inline_blob is not None:
            raise AuditIntegrityError("custody-safe pipeline failed canonical revalidation")
        repeated_coverage = (
            claim_evaluator(safe_candidate.result.updated_state, claimed_deferred_intent_ids)
            if claimed_deferred_intent_ids and claim_evaluator is not None
            else ()
        )
        if repeated_coverage != covered_deferred_intent_ids:
            raise AuditIntegrityError("custody-safe pipeline changed deferred intent coverage")
        custody_result = "ready"

    return PipelinePlanResult(
        proposal=PipelineProposal.create(
            pipeline=safe_pipeline,
            base=base,
            reviewed_facts=reviewed_facts,
            surface=surface,
            repair_count=repair_count,
            skill_hash=skill_hash,
            covered_deferred_intent_ids=covered_deferred_intent_ids,
            supersedes_draft_hash=supersedes_draft_hash,
        ),
        tool_call_id=tool_call_id,
        custody_result=custody_result,
        model_identifier=model_identifier,
        model_version=model_version,
        provider=provider,
    )


async def prepare_pipeline_plan(
    *,
    pipeline: Mapping[str, Any],
    current_state: CompositionState,
    reviewed_facts: Mapping[str, Any],
    reviewed_planner_context: Mapping[str, Any],
    supersedes_draft_hash: str | None,
    surface: PlannerSurface,
    policy_catalog: PolicyCatalogView,
    plugin_snapshot: PluginAvailabilitySnapshot,
    originating_message: PlannerOriginatingMessage,
    base: ProposalBase,
    rendered_skill: str,
    tool_call_id: str,
    model_identifier: str,
    model_version: str,
    provider: str,
    repair_count: int,
    timeout_seconds: float,
    custody_config: PlannerCustodyConfig,
) -> PipelinePlanResult:
    """Prepare a server-derived pipeline through the planner's final gate."""

    if policy_catalog.snapshot is not plugin_snapshot:
        raise ValueError("plugin_snapshot_catalog_mismatch")
    # Server-derived plans do not send this context to a provider, but accept
    # the same explicit authority split as the model-driven entry point.
    canonical_json(reviewed_planner_context)
    if type(rendered_skill) is not str or not rendered_skill.strip():
        raise ValueError("rendered_skill must be a non-empty exact string")
    if type(repair_count) is not int or repair_count < 0:
        raise ValueError("repair_count must be a non-negative exact integer")
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    async def bounded(func: Callable[..., Any], *args: Any) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PipelinePlannerError("pipeline preparation timed out", code="TIMEOUT")
        try:
            return await asyncio.wait_for(run_sync_in_worker(func, *args), timeout=remaining)
        except TimeoutError as exc:
            raise PipelinePlannerError("pipeline preparation timed out", code="TIMEOUT") from exc

    validation = await bounded(policy_catalog.validate_composition_state, current_state)
    skill_hash = hashlib.sha256(rendered_skill.encode("utf-8")).hexdigest()
    context = ToolContext(
        catalog=policy_catalog,
        plugin_snapshot=plugin_snapshot,
        data_dir=custody_config.data_dir,
        require_data_dir_for_paths=True,
        session_engine=custody_config.session_engine,
        session_id=originating_message.session_id,
        secret_service=custody_config.secret_service,
        user_id=originating_message.user_id,
        baseline=current_state,
        current_validation=validation.validation,
        runtime_preflight=custody_config.runtime_preflight,
        max_blob_storage_per_session_bytes=custody_config.max_storage_per_session,
        user_message_id=originating_message.message_id,
        user_message_content=originating_message.content,
        composer_model_identifier=model_identifier,
        composer_model_version=model_version,
        composer_provider=provider,
        composer_skill_hash=skill_hash,
        tool_arguments_hash=None,
        reviewed_source_authority=resolve_reviewed_source_authority(
            engine=custody_config.session_engine,
            session_id=originating_message.session_id,
            user_id=originating_message.user_id,
            reviewed_facts=reviewed_facts,
            expected_reviewed_anchor_hash=reviewed_anchor_hash(reviewed_facts),
        ),
    )
    try:
        return await _build_valid_pipeline_plan(
            pipeline=pipeline,
            current_state=current_state,
            base=base,
            reviewed_facts=reviewed_facts,
            claimed_deferred_intent_ids=(),
            claim_evaluator=None,
            supersedes_draft_hash=supersedes_draft_hash,
            surface=surface,
            repair_count=repair_count,
            skill_hash=skill_hash,
            tool_call_id=tool_call_id,
            terminal_context=context,
            custody_config=custody_config,
            originating_message=originating_message,
            run_sync=bounded,
            model_identifier=model_identifier,
            model_version=model_version,
            provider=provider,
        )
    except _PipelineCandidateRejected as exc:
        # Carry the rejection's closed codes exactly as the model-driven
        # exhaustion path does (``_rejection_exhausted``). Discarding them here
        # made the server-derived path structurally undiagnosable: the route
        # recorded VALIDATION_FAILED with rejection_codes=[] while a coded
        # rejection existed, so the durable disposition named no cause at all
        # and a policy refusal was indistinguishable from a wiring mistake
        # (guided S3 investigation, 2026-07-31).
        raise PipelinePlannerError(
            "server-derived pipeline failed candidate validation",
            code="VALIDATION_FAILED",
            detail_codes=_candidate_rejection_codes(exc.result),
        ) from exc


async def plan_pipeline(
    *,
    intent: str,
    current_state: CompositionState,
    provider_current_state: Mapping[str, Any],
    reviewed_facts: Mapping[str, Any],
    reviewed_planner_context: Mapping[str, Any],
    unproducible_output_fields: tuple[str, ...],
    eligible_deferred_intent_ids: tuple[str, ...],
    claim_evaluator: PipelineClaimEvaluator | None,
    supersedes_draft_hash: str | None,
    surface: PlannerSurface,
    profile: str,
    policy_catalog: PolicyCatalogView,
    plugin_snapshot: PluginAvailabilitySnapshot,
    originating_message: PlannerOriginatingMessage,
    base: ProposalBase,
    model_config: PlannerModelConfig,
    rendered_skill: str,
    repair_budget: int,
    budget_policy: PlannerBudgetPolicy,
    custody_config: PlannerCustodyConfig,
    lifecycle: PlannerRequestLifecycle,
    recorder: BufferingRecorder,
    candidate_finalizer: PipelineCandidateFinalizer,
) -> PipelinePlanResult:
    """Plan and validate one proposal without publishing state or DB rows."""
    if type(intent) is not str or not intent.strip():
        raise ValueError("intent must be a non-empty exact string")
    if type(rendered_skill) is not str or not rendered_skill.strip():
        raise ValueError("rendered_skill must be a non-empty exact string")
    if type(repair_budget) is not int or repair_budget < 0:
        raise ValueError("repair_budget must be a non-negative exact integer")
    if profile not in {"ordinary", "tutorial"}:
        raise ValueError("profile must be 'ordinary' or 'tutorial'")
    if policy_catalog.snapshot is not plugin_snapshot:
        raise ValueError("plugin_snapshot_catalog_mismatch")
    canonical_json(provider_current_state)
    if not callable(candidate_finalizer):
        raise TypeError("candidate_finalizer must be callable")
    if type(unproducible_output_fields) is not tuple or any(type(field) is not str for field in unproducible_output_fields):
        raise TypeError("unproducible_output_fields must be an exact string tuple")
    if type(eligible_deferred_intent_ids) is not tuple or any(type(intent_id) is not str for intent_id in eligible_deferred_intent_ids):
        raise TypeError("eligible_deferred_intent_ids must be an exact string tuple")
    if len(set(eligible_deferred_intent_ids)) != len(eligible_deferred_intent_ids):
        raise ValueError("eligible_deferred_intent_ids must be unique")
    if surface in {PlannerSurface.FREEFORM, PlannerSurface.GUIDED_FULL} and eligible_deferred_intent_ids:
        raise ValueError("freeform and guided-full surfaces cannot provide eligible deferred intent ids")
    if claim_evaluator is not None and not callable(claim_evaluator):
        raise TypeError("claim_evaluator must be callable or None")
    if eligible_deferred_intent_ids and claim_evaluator is None:
        raise ValueError("eligible deferred intent claims require claim_evaluator")
    if surface in {PlannerSurface.FREEFORM, PlannerSurface.GUIDED_FULL} and claim_evaluator is not None:
        raise ValueError("freeform and guided-full surfaces cannot provide claim_evaluator")

    llm_call_start = len(recorder.llm_calls)
    outcome: PlannerSettlement = "failed"
    primary_error: BaseException | None = None
    # The guided write fence carries the operation identity; freeform has
    # none. Reused here so the trail correlates with the durable operation
    # rows without widening the planner signature.
    trail = _PlannerAttemptTrail(
        session_id=originating_message.session_id,
        operation_id=(custody_config.write_fence.operation_id if custody_config.write_fence is not None else None),
        surface=surface.value,
    )
    try:
        await lifecycle.before_start()
        async with lifecycle.request_scope():
            proposal = await _plan_pipeline_inner(
                trail=trail,
                intent=intent,
                current_state=current_state,
                provider_current_state=provider_current_state,
                reviewed_facts=reviewed_facts,
                reviewed_planner_context=reviewed_planner_context,
                unproducible_output_fields=unproducible_output_fields,
                eligible_deferred_intent_ids=eligible_deferred_intent_ids,
                claim_evaluator=claim_evaluator,
                supersedes_draft_hash=supersedes_draft_hash,
                surface=surface,
                profile=profile,
                policy_catalog=policy_catalog,
                plugin_snapshot=plugin_snapshot,
                originating_message=originating_message,
                base=base,
                model_config=model_config,
                rendered_skill=rendered_skill,
                repair_budget=repair_budget,
                budget_policy=budget_policy,
                custody_config=custody_config,
                lifecycle=lifecycle,
                recorder=recorder,
                candidate_finalizer=candidate_finalizer,
            )
        outcome = "complete"
        trail.log_summary("accepted")
        return proposal
    except BaseException as exc:
        primary_error = exc
        attach_llm_calls(exc, recorder, start_index=llm_call_start)
        if isinstance(exc, asyncio.CancelledError):
            outcome = "cancelled"
            trail.log_summary("cancelled")
        elif isinstance(exc, PlannerDeclined):
            trail.log_summary("declined")
        elif isinstance(exc, PipelinePlannerError):
            trail.log_summary(exc.code)
        else:
            # Bounded: a type name, never message content.
            trail.log_summary(type(exc).__name__)
        raise
    finally:
        try:
            await _settle_lifecycle(lifecycle, outcome)
        except BaseException as settlement_error:
            if primary_error is None:
                raise
            primary_error.add_note(f"planner lifecycle settlement also failed ({type(settlement_error).__name__})")


async def _plan_pipeline_inner(
    *,
    trail: _PlannerAttemptTrail,
    intent: str,
    current_state: CompositionState,
    provider_current_state: Mapping[str, Any],
    reviewed_facts: Mapping[str, Any],
    reviewed_planner_context: Mapping[str, Any],
    unproducible_output_fields: tuple[str, ...],
    eligible_deferred_intent_ids: tuple[str, ...],
    claim_evaluator: PipelineClaimEvaluator | None,
    supersedes_draft_hash: str | None,
    surface: PlannerSurface,
    profile: str,
    policy_catalog: PolicyCatalogView,
    plugin_snapshot: PluginAvailabilitySnapshot,
    originating_message: PlannerOriginatingMessage,
    base: ProposalBase,
    model_config: PlannerModelConfig,
    rendered_skill: str,
    repair_budget: int,
    budget_policy: PlannerBudgetPolicy,
    custody_config: PlannerCustodyConfig,
    lifecycle: PlannerRequestLifecycle,
    recorder: BufferingRecorder,
    candidate_finalizer: PipelineCandidateFinalizer,
) -> PipelinePlanResult:
    skill_hash = hashlib.sha256(rendered_skill.encode("utf-8")).hexdigest()
    deadline = asyncio.get_running_loop().time() + model_config.timeout_seconds

    async def run_planner_sync(func: Callable[..., Any], *args: Any) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PipelinePlannerError("planner wall-clock budget exhausted", code="TIMEOUT")
        try:
            return await asyncio.wait_for(run_sync_in_worker(func, *args), timeout=remaining)
        except TimeoutError as exc:
            raise PipelinePlannerError("planner wall-clock budget exhausted", code="TIMEOUT") from exc

    current_validation = await run_planner_sync(policy_catalog.validate_composition_state, current_state)
    # Server-rendered worked exemplars from the live policy-visible catalog.
    # This is the reviewed-context channel for deployment plugin facts — the
    # static skill pack must never carry them (no_deployment_plugin_facts
    # gate), and the exemplar objects are CI-validated through
    # build_set_pipeline_candidate so they cannot drift from the schemas they
    # teach. Memoized per snapshot hash; a cold build sweeps the catalog, so
    # it runs off-loop like the other sync planner phases.
    authoring_aids = await run_planner_sync(build_planner_authoring_aids, policy_catalog)
    request_context = ToolContext(
        catalog=policy_catalog,
        plugin_snapshot=plugin_snapshot,
        data_dir=custody_config.data_dir,
        require_data_dir_for_paths=True,
        session_engine=custody_config.session_engine,
        session_id=originating_message.session_id,
        secret_service=custody_config.secret_service,
        user_id=originating_message.user_id,
        baseline=current_state,
        current_validation=current_validation.validation,
        runtime_preflight=custody_config.runtime_preflight,
        max_blob_storage_per_session_bytes=custody_config.max_storage_per_session,
        user_message_id=originating_message.message_id,
        user_message_content=originating_message.content,
        composer_model_identifier=model_config.model_identifier,
        composer_model_version=model_config.model_identifier,
        composer_provider=model_config.provider,
        composer_skill_hash=skill_hash,
        tool_arguments_hash=None,
        reviewed_source_authority=resolve_reviewed_source_authority(
            engine=custody_config.session_engine,
            session_id=originating_message.session_id,
            user_id=originating_message.user_id,
            reviewed_facts=reviewed_facts,
            expected_reviewed_anchor_hash=reviewed_anchor_hash(reviewed_facts),
        ),
    )
    tools = planner_tool_definitions()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": rendered_skill},
        {
            "role": "user",
            "content": canonical_json(
                {
                    "intent": intent,
                    "current_state": provider_current_state,
                    "reviewed_facts": reviewed_planner_context,
                    "authoring_aids": authoring_aids,
                    "instruction": (
                        "Use read-only discovery as needed, then call emit_pipeline_proposal exactly once "
                        "with one complete canonical set_pipeline argument object."
                    ),
                }
            ),
        },
    ]
    total_calls = 0
    total_cost = Decimal("0")
    discovery_turns = 0
    composition_turns = 0
    repair_count = 0
    prose_nudges = 0
    nodeless_nudge_given = False
    threshold_nudge_given = False
    # Computed once: the instruction is fixed for the whole planning request.
    stated_threshold = _stated_threshold_in(intent)
    seen_discovery: set[tuple[str, str]] = set()
    seen_discovery_round = 0
    # (component, code) fingerprints of every candidate rejection so far in
    # this request. A repeat means the intervening repair changed nothing that
    # mattered — the feedback then says so explicitly (repeat_notice) instead
    # of letting the model burn budget without knowing it is looping.
    seen_rejection_fingerprints: set[tuple[tuple[str, str], ...]] = set()

    async def call_model(
        *,
        model_override: str | None = None,
        tools_override: list[dict[str, Any]] | None = None,
        allow_text_reply: bool = False,
    ) -> tuple[Any, tuple[_ParsedToolCall, ...], ComposerLLMCall]:
        nonlocal total_calls, total_cost
        effective_model = model_override or model_config.model_identifier
        active_tools = tools if tools_override is None else tools_override
        cache_marked_messages, cache_marked_tools = (
            apply_anthropic_cache_markers(messages, active_tools)
            if supports_anthropic_prompt_cache_markers(effective_model)
            else (list(messages), list(active_tools))
        )
        assert cache_marked_tools is not None
        call_input_snapshot = json.loads(
            canonical_json(
                {
                    "messages": cache_marked_messages,
                    "tools": cache_marked_tools,
                }
            )
        )
        if type(call_input_snapshot) is not dict:
            raise AuditIntegrityError("planner call input snapshot must be an exact object")
        marked_messages = cast(list[dict[str, Any]], call_input_snapshot["messages"])
        marked_tools = cast(list[dict[str, Any]], call_input_snapshot["tools"])
        manifest = build_planner_capability_manifest(
            surface=surface,
            profile=profile,
            messages=marked_messages,
            tools=marked_tools,
            canonical_schema=canonical_set_pipeline_schema(),
            tool_surface="full" if tools_override is None else "terminal_only",
        )
        request_size = len(canonical_json({"messages": marked_messages, "tools": marked_tools}).encode("utf-8"))
        if request_size > budget_policy.max_request_bytes:
            raise PipelinePlannerError("planner request byte budget exhausted", code="REQUEST_BYTES_EXHAUSTED")
        await emit_progress(lifecycle.progress, model_call_progress_event(intent))

        def record_provider_failure(
            exc: BaseException,
            status: ComposerLLMCallStatus,
            *,
            started_at: datetime,
            started_ns: int,
            ordinal: int,
        ) -> None:
            failed_call = build_llm_call_record(
                model_requested=effective_model,
                messages=marked_messages,
                tools=marked_tools,
                status=status,
                started_at=started_at,
                started_ns=started_ns,
                temperature=model_config.temperature,
                seed=model_config.seed,
                error_class=type(exc).__name__,
                error_message=type(exc).__name__,
                max_completion_tokens_requested=budget_policy.max_completion_tokens,
                planner_policy_hash=budget_policy.audit_hash,
                planner_call_ordinal=ordinal,
            )
            _assert_planner_call_matches_manifest(failed_call, manifest, recorder)
            recorder.record_llm_call(failed_call)

        for attempt in range(1, model_config.max_api_attempts + 1):
            if total_calls >= budget_policy.max_total_provider_calls:
                raise PipelinePlannerError("planner provider call budget exhausted", code="PROVIDER_CALLS_EXHAUSTED")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise PipelinePlannerError("planner wall-clock budget exhausted", code="TIMEOUT")
            total_calls += 1
            ordinal = total_calls
            started_at = datetime.now(UTC)
            started_ns = time.monotonic_ns()
            response: Any = None
            kwargs: dict[str, Any] = {
                "model": effective_model,
                "messages": marked_messages,
                "tools": marked_tools,
                "max_tokens": budget_policy.max_completion_tokens,
                # The planner loop is the sole retry owner. LiteLLM accepts
                # both spellings and gives num_retries precedence; pin both
                # to zero so every physical attempt consumes one audited
                # ordinal and one provider-call budget unit.
                "num_retries": 0,
                "max_retries": 0,
            }
            if model_config.temperature is not None:
                kwargs["temperature"] = model_config.temperature
            if model_config.seed is not None:
                kwargs["seed"] = model_config.seed
            # Endpoint affordance: select by the SAME condition that selects
            # effective_model above (model_override set == hatch turn), so
            # the escape-hatch call never lands on the primary's endpoint —
            # the two roles are independent by design.
            if model_override is not None:
                if model_config.escape_hatch_api_base is not None:
                    kwargs["api_base"] = model_config.escape_hatch_api_base
                if model_config.escape_hatch_api_key is not None:
                    kwargs["api_key"] = model_config.escape_hatch_api_key
            else:
                if model_config.api_base is not None:
                    kwargs["api_base"] = model_config.api_base
                if model_config.api_key is not None:
                    kwargs["api_key"] = model_config.api_key

            try:
                response = await asyncio.wait_for(model_config.completion(**kwargs), timeout=remaining)
            except asyncio.CancelledError as exc:
                cancelled_call = build_llm_call_record(
                    model_requested=effective_model,
                    messages=marked_messages,
                    tools=marked_tools,
                    status=ComposerLLMCallStatus.CANCELLED,
                    started_at=started_at,
                    started_ns=started_ns,
                    temperature=model_config.temperature,
                    seed=model_config.seed,
                    error_class=type(exc).__name__,
                    error_message=type(exc).__name__,
                    max_completion_tokens_requested=budget_policy.max_completion_tokens,
                    planner_policy_hash=budget_policy.audit_hash,
                    planner_call_ordinal=ordinal,
                )
                _assert_planner_call_matches_manifest(cancelled_call, manifest, recorder)
                recorder.record_llm_call(cancelled_call)
                raise
            except TimeoutError as exc:
                timed_out_call = build_llm_call_record(
                    model_requested=effective_model,
                    messages=marked_messages,
                    tools=marked_tools,
                    status=ComposerLLMCallStatus.TIMEOUT,
                    started_at=started_at,
                    started_ns=started_ns,
                    temperature=model_config.temperature,
                    seed=model_config.seed,
                    error_class=type(exc).__name__,
                    error_message=type(exc).__name__,
                    max_completion_tokens_requested=budget_policy.max_completion_tokens,
                    planner_policy_hash=budget_policy.audit_hash,
                    planner_call_ordinal=ordinal,
                )
                _assert_planner_call_matches_manifest(timed_out_call, manifest, recorder)
                recorder.record_llm_call(timed_out_call)
                raise PipelinePlannerError("planner wall-clock budget exhausted", code="TIMEOUT") from exc
            except LiteLLMAuthError as exc:
                record_provider_failure(
                    exc,
                    ComposerLLMCallStatus.AUTH_ERROR,
                    started_at=started_at,
                    started_ns=started_ns,
                    ordinal=ordinal,
                )
                raise PipelinePlannerError(
                    f"planner provider call failed ({type(exc).__name__})",
                    code="PROVIDER_ERROR",
                ) from None
            except LiteLLMBadRequestError as exc:
                record_provider_failure(
                    exc,
                    ComposerLLMCallStatus.BAD_REQUEST_ERROR,
                    started_at=started_at,
                    started_ns=started_ns,
                    ordinal=ordinal,
                )
                raise PipelinePlannerError(
                    f"planner provider call failed ({type(exc).__name__})",
                    code="PROVIDER_ERROR",
                ) from None
            except LiteLLMAPIError as exc:
                record_provider_failure(
                    exc,
                    ComposerLLMCallStatus.API_ERROR,
                    started_at=started_at,
                    started_ns=started_ns,
                    ordinal=ordinal,
                )
                if attempt < model_config.max_api_attempts:
                    retry_delay = model_config.api_retry_base_seconds * (2 ** (attempt - 1))
                    if retry_delay > 0:
                        await asyncio.sleep(min(retry_delay, max(0.0, deadline - asyncio.get_running_loop().time())))
                    continue
                raise PipelinePlannerError(
                    f"planner provider call failed ({type(exc).__name__})",
                    code="PROVIDER_ERROR",
                ) from None

            call = build_llm_call_record(
                model_requested=effective_model,
                messages=marked_messages,
                tools=marked_tools,
                status=ComposerLLMCallStatus.SUCCESS,
                started_at=started_at,
                started_ns=started_ns,
                temperature=model_config.temperature,
                seed=model_config.seed,
                response=response,
                max_completion_tokens_requested=budget_policy.max_completion_tokens,
                planner_policy_hash=budget_policy.audit_hash,
                planner_call_ordinal=ordinal,
            )
            _assert_planner_call_matches_manifest(call, manifest, recorder)
            # Cost enforcement is intentionally post-call and pre-parse.  Do
            # not inspect provider content or dispatch tools before it passes.
            if call.provider_cost is None:
                recorder.record_llm_call(call)
                raise PipelinePlannerError("planner provider cost metadata is missing or malformed", code="COST_UNAVAILABLE")
            if call.completion_tokens is None:
                malformed_usage = PipelinePlannerError(
                    "planner completion token metadata is missing or malformed",
                    code="MALFORMED_RESPONSE",
                )
                recorder.record_llm_call(
                    replace(
                        call,
                        status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
                        error_class=type(malformed_usage).__name__,
                        error_message=malformed_usage.code,
                    )
                )
                raise malformed_usage
            if call.completion_tokens > budget_policy.max_completion_tokens:
                recorder.record_llm_call(call)
                raise PipelinePlannerError(
                    "planner provider reported a completion token limit overage",
                    code="COMPLETION_TOKENS_EXCEEDED",
                )
            total_cost += Decimal(str(call.provider_cost))
            if total_cost > budget_policy.max_cumulative_provider_cost:
                recorder.record_llm_call(call)
                raise PipelinePlannerError("planner provider cost continuation cap exceeded", code="COST_CAP_EXCEEDED")
            try:
                parsed_response = _parse_response_tool_calls(
                    response,
                    max_tool_calls=model_config.max_tool_calls_per_turn,
                    allow_text=allow_text_reply,
                )
            except PipelinePlannerError as exc:
                if exc.code not in ("MALFORMED_RESPONSE", "PROSE_REPLY"):
                    raise
                # A response that consumed the whole completion budget and
                # failed to parse was almost certainly cut off mid-write —
                # that is a capacity event, not malformed output, and the
                # loop can repair it by asking for a more compact reply.
                truncated = call.completion_tokens is not None and call.completion_tokens >= budget_policy.max_completion_tokens
                recorder.record_llm_call(
                    replace(
                        call,
                        status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
                        error_class=type(exc).__name__,
                        error_message="RESPONSE_TRUNCATED" if truncated else exc.code,
                    )
                )
                if truncated:
                    raise PipelinePlannerError(
                        "planner response was truncated at the completion token limit",
                        code="RESPONSE_TRUNCATED",
                    ) from exc
                raise
            recorder.record_llm_call(call)
            message, calls = parsed_response
            return message, calls, call
        raise AssertionError("provider attempt loop exited without return or exception")

    # ── Escape-hatch state ────────────────────────────────────────────────
    # On budget exhaustion, instead of failing immediately, one overtime turn
    # runs on the senior advisor model with the terminal tool only. A text
    # reply on that turn is an honest decline (PlannerDeclined); anything
    # other than one clean accepted proposal re-raises the original error.
    hatch_error: PipelinePlannerError | None = None
    hatch_turn_next = False
    hatch_spent = False
    # Closed validation codes of the most recent candidate rejection, recorded
    # on any resulting exhaustion so the durable disposition names the wall.
    last_rejection_codes: tuple[str, ...] = ()

    def _rejection_exhausted() -> PipelinePlannerError:
        return PipelinePlannerError(
            "planner repair budget exhausted",
            code="REPAIR_EXHAUSTED",
            detail_codes=last_rejection_codes,
            # Carried whenever the request HAD a gap, not only when the final
            # rejection named it: the planner was asked to close this gap and
            # did not, so it is the actionable cause of the exhaustion whatever
            # code the last candidate happened to trip (R2-F4).
            unproducible_output_fields=unproducible_output_fields,
        )

    def _hatch_available() -> bool:
        return model_config.escape_hatch_model is not None and not hatch_spent

    def _engage_escape_hatch(error: PipelinePlannerError, *, rejection_feedback: Mapping[str, Any] | None = None) -> None:
        # The over-budget attempt is dropped from the conversation so no
        # assistant tool_calls message dangles without its tool results.
        # When the exhaustion was a candidate rejection, the advisor would
        # otherwise never learn WHY the final candidate failed (the dropped
        # attempt takes its feedback with it) — so the same allowlisted,
        # message-stripped envelope the repair turn would have received rides
        # inside the hatch notice. Closed codes and candidate-authored names
        # only; no new disclosure surface.
        nonlocal hatch_error, hatch_turn_next, hatch_spent
        hatch_error = error
        hatch_turn_next = True
        hatch_spent = True
        content = _escape_hatch_notice()
        if rejection_feedback is not None:
            content += "\n\nFor context: the final candidate (dropped from the conversation above) was rejected with:\n" + canonical_json(
                rejection_feedback
            )
        messages.append({"role": "user", "content": content})

    while True:
        is_hatch_turn = hatch_turn_next
        hatch_turn_next = False
        try:
            if is_hatch_turn:
                assert model_config.escape_hatch_model is not None
                assert hatch_error is not None
                message, calls, audited_call = await call_model(
                    model_override=model_config.escape_hatch_model,
                    tools_override=[planner_terminal_tool_definition()],
                    allow_text_reply=True,
                )
            else:
                message, calls, audited_call = await call_model()
        except PipelinePlannerError as exc:
            if exc.code == "PROSE_REPLY":
                # A no-tool-call reply (thinking aloud) mid-plan: nudge the
                # model back to tool calling on its own bounded budget —
                # separate from the repair budget, which answers candidate
                # rejections. Past the budget, the original terminal
                # MALFORMED_RESPONSE disposition stands unchanged.
                trail.begin_attempt()
                if is_hatch_turn:
                    # The advisor's one shot produced nothing usable: the
                    # hatch is spent, the original exhaustion stands.
                    trail.log_attempt("hatch", "prose_reply", led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                prose_nudges += 1
                if prose_nudges > _PROSE_NUDGE_BUDGET:
                    trail.log_attempt("prose", "prose_reply", planner_code="MALFORMED_RESPONSE", led_to="terminal")
                    raise PipelinePlannerError(
                        "planner response must call a declared tool",
                        code="MALFORMED_RESPONSE",
                    ) from exc
                trail.log_attempt("prose", "prose_nudged", led_to="continue")
                messages.append({"role": "user", "content": _prose_reply_notice()})
                continue
            if exc.code != "RESPONSE_TRUNCATED":
                raise
            trail.begin_attempt()
            if is_hatch_turn:
                # The advisor's one shot overflowed: the hatch is spent, the
                # original exhaustion stands.
                trail.log_attempt("hatch", "truncated", led_to="terminal")
                assert hatch_error is not None
                raise hatch_error from None
            repair_count += 1
            if repair_count > repair_budget:
                if _hatch_available():
                    trail.log_attempt("repair", "truncated", planner_code="REPAIR_EXHAUSTED", led_to="hatch")
                    _engage_escape_hatch(PipelinePlannerError("planner repair budget exhausted", code="REPAIR_EXHAUSTED"))
                    continue
                trail.log_attempt("repair", "truncated", planner_code="REPAIR_EXHAUSTED", led_to="terminal")
                raise PipelinePlannerError("planner repair budget exhausted", code="REPAIR_EXHAUSTED") from None
            trail.log_attempt("repair", "truncated", led_to="repair")
            messages.append({"role": "user", "content": _truncated_response_notice()})
            continue
        trail.begin_attempt()
        # Phase of a terminal-tool turn: the first candidate is "candidate",
        # every post-rejection retry is "repair"; the advisor turn is "hatch".
        attempt_phase = "hatch" if is_hatch_turn else ("candidate" if repair_count == 0 else "repair")
        if is_hatch_turn and not calls:
            decline_content = _provider_field(message, "content")
            trail.log_attempt("hatch", "declined", led_to="terminal")
            raise PlannerDeclined(
                "planner escape-hatch advisor declined the request",
                decline_text=decline_content if type(decline_content) is str else "",
            )
        if len(calls) > model_config.max_tool_calls_per_turn:
            trail.log_attempt(
                attempt_phase, "budget_exhausted", planner_code="TOOL_CALLS_EXHAUSTED", led_to="terminal", tool_calls=len(calls)
            )
            raise PipelinePlannerError("planner per-turn tool call budget exhausted", code="TOOL_CALLS_EXHAUSTED")

        terminal_calls = tuple(call for call in calls if call.name == _TERMINAL_TOOL_NAME)
        if terminal_calls:
            if not is_hatch_turn:
                composition_turns += 1
                if composition_turns > model_config.max_composition_turns:
                    if _hatch_available():
                        trail.log_attempt(attempt_phase, "budget_exhausted", planner_code="COMPOSITION_EXHAUSTED", led_to="hatch")
                        _engage_escape_hatch(
                            PipelinePlannerError("planner composition turn budget exhausted", code="COMPOSITION_EXHAUSTED")
                        )
                        continue
                    trail.log_attempt(attempt_phase, "budget_exhausted", planner_code="COMPOSITION_EXHAUSTED", led_to="terminal")
                    raise PipelinePlannerError("planner composition turn budget exhausted", code="COMPOSITION_EXHAUSTED")
            call = terminal_calls[0]
            terminal_feedback: Mapping[str, Any] | None = None
            # Only fingerprinted feedback kinds can repeat; schema and
            # deferred-claim feedback carry no rejection identity to compare.
            repeated_terminal_fingerprint = False
            pipeline: dict[str, Any] | None = None
            claimed_deferred_intent_ids: tuple[str, ...] = ()
            allowed_terminal_keys = {"pipeline", "claimed_deferred_intent_ids"}
            if "pipeline" not in call.arguments or set(call.arguments) - allowed_terminal_keys:
                terminal_feedback = _canonical_schema_feedback()
            else:
                try:
                    payload = _PlannerTerminalPayload.model_validate(deep_thaw(call.arguments))
                    SetPipelineArgumentsModel.model_validate(payload.pipeline)
                except ValueError as exc:
                    claim_shape_error = isinstance(exc, PydanticValidationError) and any(
                        error["loc"] and error["loc"][0] == "claimed_deferred_intent_ids" for error in exc.errors()
                    )
                    terminal_feedback = _deferred_intent_claim_feedback() if claim_shape_error else _canonical_schema_feedback()
                else:
                    pipeline = payload.pipeline
                    claimed_deferred_intent_ids = tuple(str(intent_id) for intent_id in payload.claimed_deferred_intent_ids)
                    if not set(claimed_deferred_intent_ids).issubset(eligible_deferred_intent_ids):
                        terminal_feedback = _deferred_intent_claim_feedback()
            finalized_pipeline: Mapping[str, Any] | None = None
            if terminal_feedback is None:
                assert pipeline is not None
                if pipeline.get("source") is None and pipeline.get("sources") is None:
                    # Fingerprinted like every other candidate rejection: a
                    # sourceless candidate re-emitted unchanged must still draw
                    # the repeat notice rather than silently burning budget.
                    missing_source = _missing_source_rejection(current_state)
                    missing_source_fingerprint = _rejection_fingerprint(missing_source)
                    repeated_terminal_fingerprint = missing_source_fingerprint in seen_rejection_fingerprints
                    seen_rejection_fingerprints.add(missing_source_fingerprint)
                    terminal_feedback = _allowlisted_candidate_feedback(missing_source, repeated_fingerprint=repeated_terminal_fingerprint)
                else:
                    try:
                        finalizer_result = candidate_finalizer(pipeline)
                    except AuditIntegrityError as exc:
                        if not str(exc).startswith(_CANDIDATE_SHAPE_INTEGRITY_PREFIX):
                            # Not a candidate-shape complaint: a genuine
                            # integrity breach stays terminal.
                            raise
                        # The reviewed-authority binder rejected the shape the
                        # planner authored — repairable in one budgeted turn,
                        # never a 500. The sourceless case is already answered
                        # above with its own code; the residue (an empty
                        # ``sources`` map, an invented component name) reaches
                        # here and gets the canonical schema complaint.
                        terminal_feedback = _canonical_schema_feedback()
                    else:
                        if type(finalizer_result) is not dict:
                            raise AuditIntegrityError("pipeline candidate finalizer must return an exact dict")
                        finalized_pipeline = finalizer_result
            if terminal_feedback is not None:
                last_rejection_codes = _feedback_error_codes(terminal_feedback)
                if is_hatch_turn:
                    trail.log_attempt(
                        "hatch",
                        "candidate_rejected",
                        codes=last_rejection_codes,
                        led_to="terminal",
                        repeated_fingerprint=repeated_terminal_fingerprint,
                    )
                    assert hatch_error is not None
                    raise hatch_error from None
                repair_count += 1
                if repair_count > repair_budget:
                    if _hatch_available():
                        trail.log_attempt(
                            attempt_phase,
                            "candidate_rejected",
                            codes=last_rejection_codes,
                            planner_code="REPAIR_EXHAUSTED",
                            led_to="hatch",
                            repeated_fingerprint=repeated_terminal_fingerprint,
                        )
                        _engage_escape_hatch(_rejection_exhausted(), rejection_feedback=terminal_feedback)
                        continue
                    trail.log_attempt(
                        attempt_phase,
                        "candidate_rejected",
                        codes=last_rejection_codes,
                        planner_code="REPAIR_EXHAUSTED",
                        led_to="terminal",
                        repeated_fingerprint=repeated_terminal_fingerprint,
                    )
                    raise _rejection_exhausted() from None
                trail.log_attempt(
                    attempt_phase,
                    "candidate_rejected",
                    codes=last_rejection_codes,
                    led_to="repair",
                    repeated_fingerprint=repeated_terminal_fingerprint,
                )
                messages.append(_assistant_tool_calls_message(message, calls))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": canonical_json(terminal_feedback),
                    }
                )
                continue
            assert finalized_pipeline is not None
            effective_provider = model_config.provider
            if is_hatch_turn:
                assert model_config.escape_hatch_provider is not None
                effective_provider = model_config.escape_hatch_provider
            terminal_context = replace(
                request_context,
                composer_model_identifier=audited_call.model_requested,
                composer_model_version=audited_call.model_returned or audited_call.model_requested,
                composer_provider=effective_provider,
            )
            try:
                if (
                    not is_hatch_turn
                    and not nodeless_nudge_given
                    and surface in (PlannerSurface.GUIDED_STAGED, PlannerSurface.TUTORIAL_PROFILE)
                    and supersedes_draft_hash is not None
                    and _transform_node_count(finalized_pipeline) == 0
                ):
                    # Revision turn (a rejected draft is superseded — the
                    # operator explicitly asked for changes) with zero
                    # transform/aggregation nodes: tutorial op 1152d7e3
                    # (2026-07-22) "converged" on exactly this shape after
                    # blind repairs — a bare passthrough whose metadata still
                    # claimed to scrape/summarize/clean. One coded nudge;
                    # re-emitting the same nodeless pipeline is the escape
                    # valve confirming deliberate pass-through intent (the
                    # 9137456ad omit-valve pattern; bounded like
                    # prose_nudges). Never fired on the hatch turn — the
                    # hatch is one clean proposal or terminal, and a nudge
                    # there guarantees failure.
                    nodeless_nudge_given = True
                    raise _PipelineCandidateRejected(_nodeless_revision_rejection(current_state))
                if unproducible_output_fields and _transform_node_count(finalized_pipeline) == 0:
                    # R2-F4 (elspeth-6e311df389). The reviewed outputs declare
                    # fields no reviewed source declares or observes, and this
                    # candidate has nothing that could produce them. The
                    # server-synthesized sketch is diverted here before it is
                    # ever built (ComposerServiceImpl.plan_guided_pipeline);
                    # without this guard a planner that answers the diverted
                    # request with the same bare pass-through re-opens the
                    # identical defect, provider-authored and sealed as a
                    # COMPLETE proposal.
                    #
                    # Unlike the two nudges around it this fires on EVERY
                    # attempt including the hatch, and has no omit-valve: the
                    # claim is a mechanical set difference over reviewed facts,
                    # not an inference from prose, and adding any transform
                    # clears it in one turn. An identical re-emit draws the
                    # ordinary repeat notice through the shared fingerprint
                    # path rather than being waved through.
                    raise _PipelineCandidateRejected(
                        _unproducible_output_fields_rejection(current_state, fields=unproducible_output_fields)
                    )
                if not is_hatch_turn and not threshold_nudge_given and stated_threshold is not None:
                    # Stated-threshold fidelity (R2-F17, elspeth-5c0c09db31).
                    # The instruction named a comparison and no gate in the
                    # candidate reads a row to apply it, so the rule was
                    # dropped: a constant gate forking to several destinations
                    # writes every row everywhere. Structurally the shape is
                    # legal — this can only be caught here, where the
                    # instruction is in scope — so it is ONE coded repair with
                    # the same omit-valve as the nodeless nudge: re-emitting
                    # the same pipeline confirms a deliberate fan-out. Never
                    # on the hatch turn, which gets one clean shot.
                    homeless_gate_id = _threshold_homeless_gate_id(finalized_pipeline)
                    if homeless_gate_id is not None:
                        threshold_nudge_given = True
                        raise _PipelineCandidateRejected(
                            _stated_threshold_ignored_rejection(
                                current_state,
                                node_id=homeless_gate_id,
                                stated=stated_threshold,
                            )
                        )
                accepted_plan = await _build_valid_pipeline_plan(
                    pipeline=finalized_pipeline,
                    current_state=current_state,
                    base=base,
                    reviewed_facts=reviewed_facts,
                    claimed_deferred_intent_ids=claimed_deferred_intent_ids,
                    claim_evaluator=claim_evaluator,
                    supersedes_draft_hash=supersedes_draft_hash,
                    surface=surface,
                    repair_count=repair_count,
                    skill_hash=skill_hash,
                    tool_call_id=call.call_id,
                    terminal_context=terminal_context,
                    custody_config=custody_config,
                    originating_message=originating_message,
                    run_sync=run_planner_sync,
                    model_identifier=audited_call.model_requested,
                    model_version=audited_call.model_returned or audited_call.model_requested,
                    provider=effective_provider,
                )
            except DeferredIntentClaimError:
                last_rejection_codes = ("deferred_intent_claim",)
                if is_hatch_turn:
                    trail.log_attempt("hatch", "deferred_claim", codes=last_rejection_codes, led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                repair_count += 1
                if repair_count > repair_budget:
                    if _hatch_available():
                        trail.log_attempt(
                            attempt_phase, "deferred_claim", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="hatch"
                        )
                        _engage_escape_hatch(_rejection_exhausted(), rejection_feedback=_deferred_intent_claim_feedback())
                        continue
                    trail.log_attempt(
                        attempt_phase, "deferred_claim", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="terminal"
                    )
                    raise _rejection_exhausted() from None
                trail.log_attempt(attempt_phase, "deferred_claim", codes=last_rejection_codes, led_to="repair")
                messages.append(_assistant_tool_calls_message(message, calls))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": canonical_json(_deferred_intent_claim_feedback()),
                    }
                )
                continue
            except ToolArgumentError as exc:
                last_rejection_codes = (exc.code or "argument_error",)
                if is_hatch_turn:
                    trail.log_attempt("hatch", "arg_error", codes=last_rejection_codes, led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                repair_count += 1
                if repair_count > repair_budget:
                    if _hatch_available():
                        trail.log_attempt(
                            attempt_phase, "arg_error", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="hatch"
                        )
                        _engage_escape_hatch(_rejection_exhausted(), rejection_feedback=_allowlisted_argument_feedback(exc))
                        continue
                    trail.log_attempt(
                        attempt_phase, "arg_error", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="terminal"
                    )
                    raise _rejection_exhausted() from None
                trail.log_attempt(attempt_phase, "arg_error", codes=last_rejection_codes, led_to="repair")
                messages.append(_assistant_tool_calls_message(message, calls))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": canonical_json(_allowlisted_argument_feedback(exc)),
                    }
                )
                continue
            except _PipelineCandidateRejected as exc:
                last_rejection_codes = _candidate_rejection_codes(exc.result)
                rejection_fingerprint = _rejection_fingerprint(exc.result)
                repeated_fingerprint = rejection_fingerprint in seen_rejection_fingerprints
                seen_rejection_fingerprints.add(rejection_fingerprint)
                candidate_feedback = _allowlisted_candidate_feedback(exc.result, repeated_fingerprint=repeated_fingerprint)
                if os.environ.get("ELSPETH_PLANNER_REJECTION_DETAIL_LOG") == "1":
                    # Operator-opted diagnostic seam. Validator messages are never
                    # logged: even an opt-in diagnostic must not persist authored
                    # option values, row content, paths, or secret material. Closed
                    # codes plus component/severity retain the useful classifier.
                    slog.warning(
                        "composer.planner_rejection_detail",
                        session_id=trail.session_id,
                        operation_id=trail.operation_id,
                        attempt=trail.attempts,
                        entries=[
                            {
                                "component": entry.component,
                                "error_code": entry.error_code or "validation_error",
                                "severity": entry.severity,
                            }
                            for entry in _rejection_entries(exc.result)
                        ],
                    )
                if is_hatch_turn:
                    trail.log_attempt("hatch", "candidate_rejected", codes=last_rejection_codes, led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                repair_count += 1
                if repair_count > repair_budget:
                    if _hatch_available():
                        trail.log_attempt(
                            attempt_phase,
                            "candidate_rejected",
                            codes=last_rejection_codes,
                            planner_code="REPAIR_EXHAUSTED",
                            led_to="hatch",
                            repeated_fingerprint=repeated_fingerprint,
                        )
                        _engage_escape_hatch(_rejection_exhausted(), rejection_feedback=candidate_feedback)
                        continue
                    trail.log_attempt(
                        attempt_phase,
                        "candidate_rejected",
                        codes=last_rejection_codes,
                        planner_code="REPAIR_EXHAUSTED",
                        led_to="terminal",
                        repeated_fingerprint=repeated_fingerprint,
                    )
                    raise _rejection_exhausted() from None
                trail.log_attempt(
                    attempt_phase,
                    "candidate_rejected",
                    codes=last_rejection_codes,
                    led_to="repair",
                    repeated_fingerprint=repeated_fingerprint,
                )
                messages.append(_assistant_tool_calls_message(message, calls))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": canonical_json(candidate_feedback),
                    }
                )
                continue
            trail.log_attempt(attempt_phase, "accepted", led_to="done", tool_calls=len(calls))
            return accepted_plan

        if is_hatch_turn:
            # The advisor did anything other than one clean terminal proposal
            # or an honest text decline: the hatch is spent, the original
            # exhaustion stands.
            assert hatch_error is not None
            raise hatch_error
        if any(call.name not in _PLANNER_DISCOVERY_TOOL_NAME_SET for call in calls):
            trail.log_attempt("discovery", "guard_fired", planner_code="DISCOVERY_ONLY", led_to="terminal", tool_calls=len(calls))
            raise PipelinePlannerError(
                "planner may execute read-only discovery tools only before its terminal proposal",
                code="DISCOVERY_ONLY",
            )
        discovery_turns += 1
        if discovery_turns > model_config.max_discovery_turns:
            if _hatch_available():
                trail.log_attempt(
                    "discovery", "budget_exhausted", planner_code="DISCOVERY_EXHAUSTED", led_to="hatch", tool_calls=len(calls)
                )
                _engage_escape_hatch(PipelinePlannerError("planner discovery turn budget exhausted", code="DISCOVERY_EXHAUSTED"))
                continue
            trail.log_attempt("discovery", "budget_exhausted", planner_code="DISCOVERY_EXHAUSTED", led_to="terminal", tool_calls=len(calls))
            raise PipelinePlannerError("planner discovery turn budget exhausted", code="DISCOVERY_EXHAUSTED")
        if repair_count != seen_discovery_round:
            # A candidate rejection opened a new repair round. The capability
            # core's discovery-order step 3 blesses re-reading discovery —
            # get_plugin_schema, get_pipeline_state, explain_validation_error —
            # against a validation rejection, so the repetition window is
            # scoped per round rather than spanning the whole request: a
            # post-rejection re-read is repair, not cycling (guided sessions
            # bad64533/b3acc846 died DISCOVERY_CYCLE mid-repair for exactly
            # this). Repetition within a single round still trips the guard
            # below, and discovery_turns / repair_budget / cost / deadline
            # remain the round-spanning backstops.
            seen_discovery.clear()
            seen_discovery_round = repair_count
        keys = tuple((call.name, stable_hash(call.arguments)) for call in calls)
        if any(key in seen_discovery for key in keys) or len(set(keys)) != len(keys):
            # A cycling planner is stuck by definition — hand the puzzle to
            # the advisor rather than failing the request.
            if _hatch_available():
                trail.log_attempt("discovery", "guard_fired", planner_code="DISCOVERY_CYCLE", led_to="hatch", tool_calls=len(calls))
                _engage_escape_hatch(PipelinePlannerError("planner discovery repetition/cycle guard fired", code="DISCOVERY_CYCLE"))
                continue
            trail.log_attempt("discovery", "guard_fired", planner_code="DISCOVERY_CYCLE", led_to="terminal", tool_calls=len(calls))
            raise PipelinePlannerError("planner discovery repetition/cycle guard fired", code="DISCOVERY_CYCLE")
        seen_discovery.update(keys)
        trail.log_attempt("discovery", "discovery_executed", led_to="continue", tool_calls=len(calls))
        await emit_progress(lifecycle.progress, tool_batch_progress_event(tuple(call.name for call in calls)))
        messages.append(_assistant_tool_calls_message(message, calls))
        for call in calls:
            await emit_progress(lifecycle.progress, tool_started_progress_event(call.name))

        async def execute_one_discovery(call: _ParsedToolCall) -> tuple[_ParsedToolCall, ToolResult]:
            dispatch = begin_dispatch(
                call.call_id,
                call.name,
                call.arguments,
                version_before=current_state.version,
                actor=originating_message.user_id or "pipeline-planner",
            )

            async def execute_discovery(call_to_execute: _ParsedToolCall = call) -> _AuditedDiscoveryResult:
                execution_arguments = cast(dict[str, Any], deep_thaw(call_to_execute.arguments))
                result = cast(
                    ToolResult,
                    await run_planner_sync(
                        execute_discovery_tool_with_context,
                        call_to_execute.name,
                        execution_arguments,
                        current_state,
                        request_context,
                    ),
                )
                if result.updated_state != current_state:
                    raise AuditIntegrityError("read-only planner discovery changed composition state")
                return _AuditedDiscoveryResult(result)

            try:
                audited = await dispatch_with_audit(
                    recorder=recorder,
                    audit=dispatch,
                    do_dispatch=execute_discovery,
                    version_after_provider=lambda carrier: carrier.result.updated_state.version,
                    arg_error_payload_factory=lambda exc: {
                        "error_class": "ToolArgumentError",
                        "error_code": exc.code or "argument_error",
                    },
                )
            except ToolArgumentError as exc:
                # A malformed discovery argument (e.g. the model guessing
                # plugin_type='node') is recoverable, exactly as the terminal
                # ARG_ERROR path is: dispatch_with_audit already recorded the
                # ARG_ERROR audit before re-raising, so feed the allowlisted
                # projection back as this call's tool result and let the model
                # repair next turn. Raising here would crash the whole request
                # as a non-PipelinePlannerError 500 with no disposition.
                feedback = _allowlisted_argument_feedback(exc)
                return call, ToolResult(
                    success=False,
                    updated_state=current_state,
                    validation=current_state.validate(),
                    affected_nodes=(),
                    data=dict(feedback),
                )
            result = cast(_AuditedDiscoveryResult, audited.result).result
            return call, result

        discovery_tasks = [asyncio.create_task(execute_one_discovery(call)) for call in calls]
        try:
            done, pending = await asyncio.wait(discovery_tasks, return_when=asyncio.FIRST_EXCEPTION)
        except BaseException:
            for task in discovery_tasks:
                if not task.done():
                    task.cancel("coordinator_cancelled")
            await asyncio.gather(*discovery_tasks, return_exceptions=True)
            raise

        primary_error: BaseException | None = None
        for task in discovery_tasks:
            if task not in done:
                continue
            try:
                task_error = task.exception()
            except asyncio.CancelledError as exc:  # pragma: no cover - only external cancellation reaches here
                task_error = exc
            if task_error is not None:
                primary_error = task_error
                break
        if primary_error is not None:
            for task in pending:
                task.cancel("sibling_failure")
            # Every dispatch owns one audit record in its finally block. Do not
            # let the planner settle or return until all sibling tasks have
            # reached that terminal recorder state. Cancelled sync workers may
            # continue privately, but their abandoned results cannot mutate the
            # recorder after this drain.
            await asyncio.gather(*discovery_tasks, return_exceptions=True)
            raise primary_error

        if pending:
            await asyncio.gather(*pending)
        discovery_results = [task.result() for task in discovery_tasks]
        for call, result in discovery_results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": _serialize_provider_discovery_result(
                        call=call,
                        result=result,
                        surface=surface,
                        provider_current_state=provider_current_state,
                    ),
                }
            )
            await emit_progress(lifecycle.progress, tool_completed_progress_event(call.name, result.success))
        remaining_discovery = model_config.max_discovery_turns - discovery_turns
        if remaining_discovery == 2:
            messages.append({"role": "user", "content": _discovery_pressure_notice(remaining_discovery)})


__all__ = [
    "PLANNER_DISCOVERY_TOOL_NAMES",
    "GuidedPlannerDecline",
    "PipelineCustodyResult",
    "PipelinePlanResult",
    "PipelinePlannerError",
    "PlannerBudgetPolicy",
    "PlannerCustodyConfig",
    "PlannerDeclined",
    "PlannerModelConfig",
    "PlannerOriginatingMessage",
    "PlannerRequestLifecycle",
    "plan_pipeline",
    "planner_terminal_tool_definition",
    "planner_tool_definitions",
    "prepare_pipeline_plan",
]
