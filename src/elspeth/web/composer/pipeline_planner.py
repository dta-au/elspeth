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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal, NotRequired, Protocol, TypedDict, cast, final
from uuid import UUID

import structlog
from jsonschema import Draft202012Validator
from litellm.exceptions import APIError as LiteLLMAPIError
from litellm.exceptions import AuthenticationError as LiteLLMAuthError
from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Engine

from elspeth.contracts.blobs import BlobGuidedOperationWriteFence
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.composer_planner_audit import (
    ComposerPlannerAttempt,
    ComposerPlannerAttemptLedTo,
    ComposerPlannerAttemptOutcome,
    ComposerPlannerAttemptPhase,
    ComposerPlannerCode,
    ComposerPlannerInformationClass,
)
from elspeth.contracts.composer_progress import ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import PluginKind
from elspeth.web.catalog.schemas import PluginSchemaInfo
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
from elspeth.web.composer.guided.planning import GuidedCandidateBindingRejected
from elspeth.web.composer.llm_response_parsing import (
    apply_anthropic_cache_markers,
    attach_llm_calls,
    build_llm_call_record,
    supports_anthropic_prompt_cache_markers,
)
from elspeth.web.composer.pipeline_custody import (
    PipelineCustodyPreparation,
    finalize_pipeline_custody,
    pending_custody_blob_view,
    prepare_pipeline_custody,
)
from elspeth.web.composer.pipeline_proposal import PipelineProposal, PlannerSurface, ProposalBase, reviewed_anchor_hash
from elspeth.web.composer.planner_authoring_aids import (
    PlannerPluginContract,
    SchemaContractProjectionUnsupported,
    build_planner_authoring_aids,
    discovery_digest_detail_tools,
    planner_plugin_contract,
)
from elspeth.web.composer.progress import (
    emit_progress,
    model_call_progress_event,
    tool_batch_progress_event,
    tool_completed_progress_event,
    tool_started_progress_event,
)
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.reasoning import apply_reasoning_kwargs
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.reviewed_source_authority import resolve_reviewed_source_authority
from elspeth.web.composer.state import (
    COMPOSER_NODE_TYPES,
    CompositionState,
    RouteDestinationFactDict,
    ValidationEntry,
    ValidationSummary,
    coalesce_reachability_facts,
    gate_condition_is_constant,
    route_destination_facts,
)
from elspeth.web.composer.tools._common import (
    COMPONENTS_WITHHELD_KEY,
    PendingCustodyBlobView,
    RuntimePreflight,
    ToolContext,
    ToolResult,
)
from elspeth.web.composer.tools._dispatch import (
    execute_discovery_tool_with_context,
    get_tool_definitions,
)
from elspeth.web.composer.tools.generation import (
    _CLOSED_VALIDATION_ERROR_CODES,
    explain_validation_code,
    explain_withheld_validation_code,
)
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate, canonicalize_authored_node_review_requirements
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_PLANNER_DISCOVERY_TOOL_NAME_SET: Final[frozenset[str]] = frozenset(PLANNER_DISCOVERY_TOOL_NAMES)
_TERMINAL_TOOL_NAME: Final[str] = PLANNER_TERMINAL_TOOL_NAME

_PIPELINE_CURRENT_INFORMATION: Final[str] = "pipeline.current"
_CATALOG_SELECTION_INFORMATION: Final[str] = "catalog.selection"
_CATALOG_DETAIL_INFORMATION_BY_TOOL: Final[Mapping[str, str]] = {
    "list_sources": "catalog.details.source",
    "list_transforms": "catalog.details.transform",
    "list_sinks": "catalog.details.sink",
}
_RECIPE_INDEX_INFORMATION: Final[str] = "recipe.index"
_FULL_STATE_ALIASES: Final[frozenset[str]] = frozenset({"", "all", "full", "pipeline"})
_ALL_INFORMATION_GAPS_CLOSED_NOTICE: Final[str] = "All declared information gaps are closed; emit the terminal proposal now."


def _valid_information_key(key: str) -> bool:
    return key in {
        _PIPELINE_CURRENT_INFORMATION,
        _CATALOG_SELECTION_INFORMATION,
        *_CATALOG_DETAIL_INFORMATION_BY_TOOL.values(),
        _RECIPE_INDEX_INFORMATION,
        "pipeline.full",
        "pipeline.source",
        "model.catalog",
        "blob.index.session",
        "blob.index.composer",
        "secret.index",
        "audit.info",
        "expression.grammar",
        "pipeline.preview",
        "pipeline.diff",
    } or key.startswith(
        (
            "pipeline.component:",
            "plugin.schema:",
            "plugin.assistance:",
            "blob.metadata:",
            "blob.inspection:",
            "blob.content:",
            "validation.code:",
            "secret.reference:",
        )
    )


@dataclass(frozen=True, slots=True)
class PlannerInformationManifest:
    """Closed request-owned facts already supplied or proven unavailable."""

    supplied: frozenset[str]
    unavailable: frozenset[str] = frozenset()
    unresolved: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if any(type(key) is not str or not _valid_information_key(key) for key in (*self.supplied, *self.unavailable, *self.unresolved)):
            raise ValueError("planner information manifest contains an unknown key")
        if self.unresolved & (self.supplied | self.unavailable):
            raise ValueError("planner information manifest cannot resolve and supply the same key")

    def covers(self, key: str) -> bool:
        if key in self.unresolved:
            return False
        if key in self.supplied or key in self.unavailable:
            return True
        if key in _CATALOG_DETAIL_INFORMATION_BY_TOOL.values():
            return _CATALOG_SELECTION_INFORMATION in self.supplied
        is_state_projection = key in {"pipeline.full", "pipeline.source"} or key.startswith("pipeline.component:")
        return is_state_projection and (_PIPELINE_CURRENT_INFORMATION in self.supplied or "pipeline.full" in self.supplied)

    def supplies(self, key: str) -> bool:
        """Return whether usable information, rather than an omission, closes key."""
        if key in self.unresolved:
            return False
        if key in self.supplied:
            return True
        if key in _CATALOG_DETAIL_INFORMATION_BY_TOOL.values():
            return _CATALOG_SELECTION_INFORMATION in self.supplied
        is_state_projection = key in {"pipeline.full", "pipeline.source"} or key.startswith("pipeline.component:")
        return is_state_projection and (_PIPELINE_CURRENT_INFORMATION in self.supplied or "pipeline.full" in self.supplied)

    def with_result(self, keys: tuple[str, ...], *, available: bool) -> PlannerInformationManifest:
        target = self.supplied if available else self.unavailable
        updated = target | frozenset(keys)
        if available:
            return PlannerInformationManifest(
                supplied=updated,
                unavailable=self.unavailable - frozenset(keys),
                unresolved=self.unresolved - frozenset(keys),
            )
        return PlannerInformationManifest(
            supplied=self.supplied,
            unavailable=updated - self.supplied,
            unresolved=self.unresolved - frozenset(keys),
        )

    def provider_payload(
        self,
        *,
        discoverable_classes: tuple[str, ...],
        unresolved_keys: frozenset[str],
    ) -> dict[str, object]:
        return {
            "supplied": {
                "pipeline_state": "current_projection",
                "plugin_selection": "policy_snapshot",
            },
            "discoverable_classes": list(discoverable_classes),
            "unresolved": sorted(unresolved_keys),
        }


@dataclass(frozen=True, slots=True)
class PlannerDiscoveryPolicy:
    """Immutable palette derived from one request's exact information gaps."""

    manifest: PlannerInformationManifest
    discovery_tool_names: tuple[str, ...]
    unresolved_classes: tuple[str, ...]

    @classmethod
    def initial(
        cls,
        surface: PlannerSurface,
        *,
        required_catalog_detail_tools: tuple[str, ...] = (),
    ) -> PlannerDiscoveryPolicy:
        unknown_detail_tools = set(required_catalog_detail_tools) - set(_CATALOG_DETAIL_INFORMATION_BY_TOOL)
        if unknown_detail_tools:
            raise ValueError("planner discovery policy contains an unknown catalog detail tool")
        unavailable = (
            frozenset({"pipeline.preview"}) if surface in {PlannerSurface.GUIDED_STAGED, PlannerSurface.TUTORIAL_PROFILE} else frozenset()
        )
        detail_gaps = frozenset(_CATALOG_DETAIL_INFORMATION_BY_TOOL[tool] for tool in required_catalog_detail_tools)
        manifest = PlannerInformationManifest(
            supplied=frozenset({_PIPELINE_CURRENT_INFORMATION, _CATALOG_SELECTION_INFORMATION}),
            unavailable=unavailable,
            unresolved=detail_gaps,
        )
        omitted = {"get_pipeline_state", *set(_CATALOG_DETAIL_INFORMATION_BY_TOOL) - set(required_catalog_detail_tools)}
        if unavailable:
            omitted.add("preview_pipeline")
        names = tuple(name for name in PLANNER_DISCOVERY_TOOL_NAMES if name not in omitted)
        return cls(
            manifest=manifest,
            discovery_tool_names=names,
            unresolved_classes=(
                "plugin.schema",
                "plugin.assistance",
                "model.catalog",
                "recipe.index",
                "blob.metadata",
                "validation.code",
                "secret.reference",
                *tuple(_CATALOG_DETAIL_INFORMATION_BY_TOOL[tool] for tool in required_catalog_detail_tools),
            ),
        )

    def with_manifest(self, manifest: PlannerInformationManifest) -> PlannerDiscoveryPolicy:
        retained = tuple(
            name for name in self.discovery_tool_names if not all(manifest.covers(key) for key in _tool_information_keys(name, {}))
        )
        return PlannerDiscoveryPolicy(
            manifest=manifest,
            discovery_tool_names=retained,
            unresolved_classes=self.unresolved_classes,
        )


def _tool_information_keys(name: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    if name == "get_pipeline_state":
        component = arguments["component"] if "component" in arguments else None
        normalized = component.strip().lower() if type(component) is str else ""
        if normalized in _FULL_STATE_ALIASES or component is None or normalized == "set_pipeline_arguments":
            return ("pipeline.full",)
        if normalized == "source":
            return ("pipeline.source",)
        return (f"pipeline.component:{component}",)
    if name in _CATALOG_DETAIL_INFORMATION_BY_TOOL:
        return (_CATALOG_SELECTION_INFORMATION, _CATALOG_DETAIL_INFORMATION_BY_TOOL[name])
    if name == "list_recipes":
        return (_RECIPE_INDEX_INFORMATION,)
    if name == "get_plugin_schema":
        plugin_type = arguments["plugin_type"] if "plugin_type" in arguments else None
        plugin_name = arguments["name"] if "name" in arguments else None
        return (f"plugin.schema:{plugin_type}/{plugin_name}",)
    if name == "get_plugin_assistance":
        issue = arguments["issue_code"] if "issue_code" in arguments else None
        plugin_type = arguments["plugin_type"] if "plugin_type" in arguments else None
        plugin_name = arguments["plugin_name"] if "plugin_name" in arguments else None
        return (f"plugin.assistance:{plugin_type}/{plugin_name}:{issue or 'general'}",)
    if name == "explain_validation_error":
        error_code = arguments["error_code"] if "error_code" in arguments else None
        error_text = arguments["error_text"] if "error_text" in arguments else None
        code = error_code or error_text or "unknown"
        return (f"validation.code:{code}",)
    if name == "list_models":
        return ("model.catalog",)
    if name == "list_blobs":
        return ("blob.index.session",)
    if name == "list_composer_blobs":
        return ("blob.index.composer",)
    if name == "get_blob_metadata":
        blob_id = arguments["blob_id"] if "blob_id" in arguments else None
        return (f"blob.metadata:{blob_id}",)
    if name == "inspect_source":
        blob_id = arguments["blob_id"] if "blob_id" in arguments else None
        return (f"blob.inspection:{blob_id}",)
    if name == "get_blob_content":
        blob_id = arguments["blob_id"] if "blob_id" in arguments else None
        return (f"blob.content:{blob_id}",)
    if name == "list_secret_refs":
        return ("secret.index",)
    if name == "validate_secret_ref":
        secret_name = arguments["name"] if "name" in arguments else None
        return (f"secret.reference:{secret_name}",)
    if name == "get_audit_info":
        return ("audit.info",)
    if name == "get_expression_grammar":
        return ("expression.grammar",)
    if name == "preview_pipeline":
        return ("pipeline.preview",)
    if name == "diff_pipeline":
        return ("pipeline.diff",)
    return ()


def planner_discovery_information_keys(call: _ParsedToolCall) -> tuple[str, ...]:
    """Map one admitted discovery call to its closed information identities."""
    return _tool_information_keys(call.name, call.arguments)


def _intent_selected_schema_keys(intent: str) -> frozenset[str]:
    """Extract only explicit kind-qualified plugin selections from a request."""
    return frozenset(
        f"plugin.schema:{kind}/{name}" for kind, name in re.findall(r"\b(source|transform|sink):([a-z0-9][a-z0-9_.-]*)\b", intent.lower())
    )


class _Completion(Protocol):
    async def __call__(self, **kwargs: Any) -> Any: ...


class PlannerPriorUserRequestDict(TypedDict):
    """Provider projection of one authoritative earlier user request."""

    history_index: int
    content: str


class PlannerConversationContextDict(TypedDict):
    """Bounded earlier-user context supplied to a referential planner turn."""

    prior_user_requests: list[PlannerPriorUserRequestDict]
    additional_prior_user_requests_omitted: int


@dataclass(frozen=True, slots=True)
class PlannerPriorUserRequest:
    """One user-authored history entry retained for planner intent custody."""

    history_index: int
    content: str

    def __post_init__(self) -> None:
        if type(self.history_index) is not int or self.history_index < 0:
            raise ValueError("history_index must be a non-negative exact integer")
        if type(self.content) is not str or not self.content.strip():
            raise ValueError("content must be a non-empty exact string")

    def to_dict(self) -> PlannerPriorUserRequestDict:
        return PlannerPriorUserRequestDict(history_index=self.history_index, content=self.content)


@dataclass(frozen=True, slots=True)
class PlannerConversationContext:
    """Owned, bounded context for resolving a referential current request."""

    prior_user_requests: tuple[PlannerPriorUserRequest, ...]
    additional_prior_user_requests_omitted: int = 0

    def __post_init__(self) -> None:
        if type(self.prior_user_requests) is not tuple or any(
            type(request) is not PlannerPriorUserRequest for request in self.prior_user_requests
        ):
            raise TypeError("prior_user_requests must be an exact PlannerPriorUserRequest tuple")
        if not self.prior_user_requests:
            raise ValueError("prior_user_requests must not be empty")
        history_indices = tuple(request.history_index for request in self.prior_user_requests)
        if history_indices != tuple(sorted(set(history_indices))):
            raise ValueError("prior_user_requests history_index values must be unique and increasing")
        if type(self.additional_prior_user_requests_omitted) is not int or self.additional_prior_user_requests_omitted < 0:
            raise ValueError("additional_prior_user_requests_omitted must be a non-negative exact integer")
        if self.additional_prior_user_requests_omitted > 0 and len(self.prior_user_requests) < 2:
            raise ValueError("omitted prior requests require both an anchor and a retained recent request")

    def to_dict(self) -> PlannerConversationContextDict:
        return PlannerConversationContextDict(
            prior_user_requests=[request.to_dict() for request in self.prior_user_requests],
            additional_prior_user_requests_omitted=self.additional_prior_user_requests_omitted,
        )


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
    """Honest decline: the model answered in text on a turn where text is legal.

    Raised from the overtime escape-hatch turn (any text — the advisor is
    tool-restricted and taught), and from an ordinary turn once the
    information manifest shows every declared or requested fact supplied or
    proven unavailable AND the reply leads with the taught
    ``_PROSE_DECLINE_MARKER`` — ordinary turns keep the full palette, so
    marker-less text stays narration and is nudged. ``decline_text`` is the
    model's own explanation (marker stripped) and is intended to be surfaced
    to the user as an ordinary assistant message, never as a provider
    failure.
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


@final
class PipelineCandidatePolicyRejection(RuntimeError):
    """Closed repairable objection raised by a surface-specific acceptance check."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("candidate policy rejection code must be a non-empty exact string")
        if explain_validation_code(error_code) is None:
            raise ValueError("candidate policy rejection code must have closed repair guidance")
        super().__init__("pipeline candidate did not satisfy a surface policy")
        self.error_code = error_code


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
    # Reasoning-effort hints (elspeth-dc459d438e). Discovery effort rides
    # ordinary full-surface turns; candidate effort rides hatch and repair
    # turns. A first-shot candidate submitted on a full-surface turn is
    # DELIBERATELY generated at discovery effort: the validation gates catch
    # a shallow first shot and every repair round re-thinks at candidate
    # effort, so quality is protected without paying candidate-level
    # latency on every tool-choreography turn.
    discovery_reasoning_effort: str
    candidate_reasoning_effort: str
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


# Routing-destination keys a finalizer pass may rewire on an EXISTING
# component while leaving the component's configuration exactly what the
# model authored. The auto-wire pass (``wire_required_controls``) splices
# control nodes by retargeting the neighbour's ``input``/``on_success`` onto
# the inserted control's streams; the guided binder can likewise rebind a
# component's routing to a reviewed PRIVATE destination without touching its
# options. The two change kinds feed different projections — validator
# ``detail`` quotes a component's OPTIONS, ``connectivity`` facts quote its
# ROUTING values — so ownership is tracked per kind
# (:class:`_FinalizerOwnedRefs`): collapsing them either re-creates the
# repair blindness this seam repairs (elspeth-5904b1683a) on any auto-wired
# candidate, or leaks a finalizer-written private routing association through
# the connectivity facts.
_FINALIZER_ROUTING_KEYS: Final[frozenset[str]] = frozenset({"input", "on_success", "on_error"})


def _component_config_identity(block: Mapping[str, Any]) -> str:
    """Canonical identity of one component block minus routing destinations."""
    return canonical_json({key: value for key, value in block.items() if key not in _FINALIZER_ROUTING_KEYS})


@dataclass(frozen=True, slots=True)
class _FinalizerOwnedRefs:
    """Validation-component refs the candidate finalizer owns, by change kind.

    ``config`` names components whose non-routing content the finalizer wrote
    (guided reviewed-authority source/output binding, correction-restored
    predecessor nodes, inserted REQUIRED controls): validator messages about
    them can quote reviewed private option values, so their entries are
    masked to component ``"pipeline"`` and stripped of every candidate fact.
    ``routing`` names components whose routing destinations alone the
    finalizer retargeted: their options — and therefore validator ``detail``
    about their options — remain exactly what the model authored, but their
    ``connectivity`` facts would quote the finalizer-written routing values,
    so only those facts are suppressed.
    """

    config: frozenset[str] = frozenset()
    routing: frozenset[str] = frozenset()

    def owns_anything(self) -> bool:
        return bool(self.config) or bool(self.routing)


_FINALIZER_OWNS_NOTHING: Final[_FinalizerOwnedRefs] = _FinalizerOwnedRefs()


@observation_boundary(
    tier=3,
    source="a set_pipeline candidate mapping (LLM tool-call arguments or its finalized projection)",
    source_param="candidate",
    suppresses=("R5",),
    invariant=(
        "silently skips any block that cannot carry a well-formed component ref (non-Mapping source/"
        "node/output block, or a missing/empty/non-string id); never raises"
    ),
)
def _candidate_component_blocks(candidate: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Map validation-component refs to their raw blocks in one candidate.

    The ref vocabulary matches state validation exactly (``source`` /
    ``source:<name>`` / ``node:<id>`` / ``output:<name>`` — see
    ``composer.state``'s component naming). Tolerant by design: a block that
    cannot carry a well-formed ref (non-mapping, missing/empty id) is skipped,
    because validation can never attribute an error entry to it either — such
    candidates resolve through the canonical-schema complaint, not through
    entry-scoped withholding.
    """
    blocks: dict[str, Mapping[str, Any]] = {}
    raw_source = candidate.get("source")
    if isinstance(raw_source, Mapping):
        blocks["source"] = raw_source
    raw_sources = candidate.get("sources")
    if isinstance(raw_sources, Mapping):
        for name, block in raw_sources.items():
            if type(name) is str and name and isinstance(block, Mapping):
                blocks["source" if name == "source" else f"source:{name}"] = block
    raw_nodes = candidate.get("nodes")
    if type(raw_nodes) is list:
        for node in raw_nodes:
            if isinstance(node, Mapping):
                node_id = node.get("id")
                if type(node_id) is str and node_id:
                    blocks[f"node:{node_id}"] = node
    raw_outputs = candidate.get("outputs")
    if type(raw_outputs) is list:
        for output in raw_outputs:
            if isinstance(output, Mapping):
                sink_name = output.get("sink_name")
                if type(sink_name) is str and sink_name:
                    blocks[f"output:{sink_name}"] = output
    return blocks


def _derive_finalizer_owned_refs(
    candidate: Mapping[str, Any],
    finalized: Mapping[str, Any],
) -> _FinalizerOwnedRefs:
    """Component refs the candidate finalizer owns, derived by structural diff.

    Derived at the planner call site rather than self-reported through the
    finalizer protocol (the seam decision for elspeth-5904b1683a): the planner
    already holds both the authored candidate and the finalized result, and a
    diff cannot under-report — a finalizer pass that forgets to declare a
    mutation would keep validator detail for a server-bound component, the
    custody leak this boundary exists to prevent. A component the finalizer
    introduced is config-owned (auto-wired REQUIRED controls); one whose
    non-routing content changed is config-owned (guided reviewed-authority
    binding, correction-restored predecessor nodes); one whose ONLY change is
    a routing retarget (see ``_FINALIZER_ROUTING_KEYS``) is routing-owned. A
    byte-identical component is owned in neither sense: everything a
    projection about it can quote, the model already emitted itself.

    The identity fast path mirrors ``wire_required_controls``'s no-op
    contract (returns the candidate object ITSELF unchanged).
    """
    if finalized is candidate:
        return _FINALIZER_OWNS_NOTHING
    authored = _candidate_component_blocks(candidate)
    config_owned: set[str] = set()
    routing_owned: set[str] = set()
    for ref, block in _candidate_component_blocks(finalized).items():
        original = authored.get(ref)
        if original is None:
            config_owned.add(ref)
        elif original is block:
            continue
        elif _component_config_identity(original) != _component_config_identity(block):
            config_owned.add(ref)
        elif canonical_json(original) != canonical_json(block):
            routing_owned.add(ref)
    return _FinalizerOwnedRefs(config=frozenset(config_owned), routing=frozenset(routing_owned))


type PipelineCandidateFinalizer = Callable[[Mapping[str, Any]], Mapping[str, Any]]
type PipelineCandidateAcceptance = Callable[[CompositionState], None]
type PipelineClaimEvaluator = Callable[[CompositionState, tuple[str, ...]], tuple[str, ...]]


def _canonical_terminal_materializer(pipeline: Mapping[str, Any]) -> Mapping[str, Any]:
    """Identity materializer for unrestricted full-document authoring."""
    return pipeline


@dataclass(frozen=True, slots=True)
class PlannerTerminalMaterialization:
    """Canonical terminal result plus server-owned feedback custody refs."""

    pipeline: Mapping[str, Any]
    config_owned_refs: frozenset[str] = frozenset()
    routing_owned_refs: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.pipeline) is not dict:
            raise TypeError("PlannerTerminalMaterialization.pipeline must be an exact dict")
        for field_name, refs in (
            ("config_owned_refs", self.config_owned_refs),
            ("routing_owned_refs", self.routing_owned_refs),
        ):
            if type(refs) is not frozenset or any(type(ref) is not str or not ref for ref in refs):
                raise TypeError(f"PlannerTerminalMaterialization.{field_name} must be an exact non-empty string frozenset")
        freeze_fields(self, "pipeline")


type PipelineTerminalMaterializer = Callable[
    [Mapping[str, Any]],
    Mapping[str, Any] | PlannerTerminalMaterialization,
]


@dataclass(frozen=True, slots=True)
class PlannerTerminalContract:
    """One request-owned advertised proposal schema and canonical materializer.

    The provider validates against ``schema``. ``materialize`` then converts
    that admitted request shape into the ordinary canonical set-pipeline
    document consumed by every existing finalizer, candidate check, custody
    step, and proposal seal.  Keeping the two values in one owned object makes
    it impossible for repair or escape-hatch turns to advertise one contract
    while parsing another.
    """

    schema: Mapping[str, Any]
    materialize: PipelineTerminalMaterializer
    instruction: str | None = None

    def __post_init__(self) -> None:
        if type(self.schema) is not dict:
            raise TypeError("PlannerTerminalContract.schema must be an exact dict")
        Draft202012Validator.check_schema(self.schema)
        canonical_json(self.schema)
        if not callable(self.materialize):
            raise TypeError("PlannerTerminalContract.materialize must be callable")
        if self.instruction is not None and (type(self.instruction) is not str or not self.instruction.strip()):
            raise TypeError("PlannerTerminalContract.instruction must be a non-empty exact string or None")
        freeze_fields(self, "schema")


def canonical_planner_terminal_contract() -> PlannerTerminalContract:
    """Return the byte-compatible full-document planner contract."""
    return PlannerTerminalContract(
        schema=dict(canonical_set_pipeline_schema()),
        materialize=_canonical_terminal_materializer,
    )


@dataclass(frozen=True, slots=True)
class PlannerCustodyConfig:
    data_dir: str
    session_engine: Engine | None
    max_storage_per_session: int
    secret_service: WebSecretResolver | None
    runtime_preflight: RuntimePreflight | None
    write_fence: BlobGuidedOperationWriteFence | None = None
    # Guided-full defers inline-custody finalization into the atomic staging
    # settlement: the blob row's composite lineage FK requires the originating
    # chat message row, which that surface only inserts at settlement
    # (elspeth-1e3ad83d89). Surfaces whose originating message already exists
    # (freeform chat) keep finalizing mid-plan.
    defer_finalize: bool = False

    def __post_init__(self) -> None:
        if type(self.data_dir) is not str or not self.data_dir.strip():
            raise ValueError("data_dir must be a non-empty exact string")
        if type(self.max_storage_per_session) is not int or self.max_storage_per_session <= 0:
            raise ValueError("max_storage_per_session must be a positive exact integer")
        if self.write_fence is not None and type(self.write_fence) is not BlobGuidedOperationWriteFence:
            raise TypeError("PlannerCustodyConfig.write_fence must be an exact BlobGuidedOperationWriteFence")
        if type(self.defer_finalize) is not bool:
            raise TypeError("PlannerCustodyConfig.defer_finalize must be an exact bool")


PlannerSettlement = Literal["complete", "failed", "cancelled"]
PipelineCustodyResult = Literal["not_required", "ready"]


slog = structlog.get_logger()


_PLANNER_INFORMATION_EXACT: Final[Mapping[str, ComposerPlannerInformationClass]] = {
    information.value: information
    for information in ComposerPlannerInformationClass
    if information
    not in {
        ComposerPlannerInformationClass.PIPELINE_COMPONENT,
        ComposerPlannerInformationClass.PLUGIN_SCHEMA,
        ComposerPlannerInformationClass.PLUGIN_ASSISTANCE,
        ComposerPlannerInformationClass.BLOB_METADATA,
        ComposerPlannerInformationClass.BLOB_INSPECTION,
        ComposerPlannerInformationClass.BLOB_CONTENT,
        ComposerPlannerInformationClass.SECRET_REFERENCE,
        ComposerPlannerInformationClass.VALIDATION_CODE,
    }
}
_PLANNER_INFORMATION_PREFIXES: Final[tuple[tuple[str, ComposerPlannerInformationClass], ...]] = (
    ("pipeline.component:", ComposerPlannerInformationClass.PIPELINE_COMPONENT),
    ("plugin.schema:", ComposerPlannerInformationClass.PLUGIN_SCHEMA),
    ("plugin.assistance:", ComposerPlannerInformationClass.PLUGIN_ASSISTANCE),
    ("blob.metadata:", ComposerPlannerInformationClass.BLOB_METADATA),
    ("blob.inspection:", ComposerPlannerInformationClass.BLOB_INSPECTION),
    ("blob.content:", ComposerPlannerInformationClass.BLOB_CONTENT),
    ("secret.reference:", ComposerPlannerInformationClass.SECRET_REFERENCE),
    ("validation.code:", ComposerPlannerInformationClass.VALIDATION_CODE),
)
_PLANNER_SERVER_REJECTION_CODES: Final[frozenset[str]] = frozenset(
    {
        "argument_error",
        "canonical_schema",
        "deferred_intent_claim",
        "validation_error",
    }
)
_PLANNER_DECLARED_TOOL_NAMES: Final[frozenset[str]] = frozenset({*PLANNER_DISCOVERY_TOOL_NAMES, PLANNER_TERMINAL_TOOL_NAME})


def _planner_information_class(key: str) -> ComposerPlannerInformationClass:
    if key in _PLANNER_INFORMATION_EXACT:
        return _PLANNER_INFORMATION_EXACT[key]
    for prefix, information_class in _PLANNER_INFORMATION_PREFIXES:
        if key.startswith(prefix):
            return information_class
    raise AuditIntegrityError("planner attempt referenced an unknown information class")


def _planner_information_classes(keys: tuple[str, ...]) -> tuple[ComposerPlannerInformationClass, ...]:
    return tuple(_planner_information_class(key) for key in keys)


def _planner_tool_name(name: str) -> str:
    return name if name in _PLANNER_DECLARED_TOOL_NAMES else "undeclared_tool"


def _closed_planner_rejection_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    closed = tuple(
        code if code in _PLANNER_SERVER_REJECTION_CODES or code in _CLOSED_VALIDATION_ERROR_CODES else "validation_error" for code in codes
    )
    return tuple(sorted(set(closed)))


def _value_free_shape(value: object) -> object:
    """Project a value to container/scalar shape without retaining any value."""
    if type(value) is dict:
        child_shapes = [_value_free_shape(child) for child in value.values()]
        return {
            "kind": "object",
            "size": len(value),
            "children": sorted(child_shapes, key=canonical_json),
        }
    if type(value) is list:
        return {
            "kind": "array",
            "size": len(value),
            "children": [_value_free_shape(child) for child in value],
        }
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if type(value) is str:
        return "string"
    return "unknown"


def _candidate_shape_hash(candidate: Mapping[str, Any]) -> str:
    """Hash only the structural shape of a model-authored candidate."""
    raw_nodes = candidate.get("nodes")
    node_types: list[str] = []
    if type(raw_nodes) is list:
        for raw_node in raw_nodes:
            node_type = raw_node["node_type"] if type(raw_node) is dict and "node_type" in raw_node else None
            node_types.append(node_type if type(node_type) is str and node_type in COMPOSER_NODE_TYPES else "unknown")
    return stable_hash(
        {
            "domain": "composer.planner.candidate-shape.v1",
            "node_types": node_types,
            "shape": _value_free_shape(deep_thaw(candidate)),
        }
    )


@dataclass(slots=True)
class _ActivePlannerAttempt:
    ordinal: int
    planner_call_ordinal: int
    phase_hint: ComposerPlannerAttemptPhase
    selected_tools: tuple[str, ...]
    requested_information: tuple[ComposerPlannerInformationClass, ...]
    candidate_shape_hash: str | None


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
      resolved the attempt (e.g. DISCOVERY_CYCLE, REPAIR_EXHAUSTED, or
      REPAIR_BLIND_REPEAT when the repeat-while-blind short-circuit fired —
      the raised error still carries code REPAIR_EXHAUSTED)
    """

    def __init__(
        self,
        *,
        session_id: str,
        operation_id: str | None,
        surface: str,
        recorder: BufferingRecorder,
    ) -> None:
        self.session_id = session_id
        self.operation_id = operation_id
        self.surface = surface
        self.attempts = 0
        self.phase_counts: dict[str, int] = {}
        self.rejection_history: list[dict[str, Any]] = []
        self._recorder = recorder
        self._active: _ActivePlannerAttempt | None = None

    def begin_attempt(
        self,
        *,
        planner_call_ordinal: int,
        phase_hint: ComposerPlannerAttemptPhase,
        selected_tools: tuple[str, ...] = (),
        requested_information: tuple[ComposerPlannerInformationClass, ...] = (),
        candidate_shape_hash: str | None = None,
    ) -> None:
        if self._active is not None:
            raise AuditIntegrityError("planner semantic attempt began before the prior attempt settled")
        self.attempts += 1
        self._active = _ActivePlannerAttempt(
            ordinal=self.attempts,
            planner_call_ordinal=planner_call_ordinal,
            phase_hint=phase_hint,
            selected_tools=selected_tools,
            requested_information=requested_information,
            candidate_shape_hash=candidate_shape_hash,
        )

    def set_active_phase(self, phase: ComposerPlannerAttemptPhase) -> None:
        if self._active is None:
            raise AuditIntegrityError("planner semantic attempt phase changed without an active attempt")
        self._active.phase_hint = phase

    def finish_attempt(
        self,
        phase: str | ComposerPlannerAttemptPhase,
        outcome: str | ComposerPlannerAttemptOutcome,
        *,
        led_to: str | ComposerPlannerAttemptLedTo,
        codes: tuple[str, ...] = (),
        planner_code: str | None = None,
        tool_calls: int = 0,
        repeated_fingerprint: bool = False,
        new_information: tuple[ComposerPlannerInformationClass, ...] = (),
    ) -> None:
        del tool_calls
        active = self._active
        if active is None:
            raise AuditIntegrityError("planner semantic attempt settled without an active attempt")
        phase_value = ComposerPlannerAttemptPhase(phase)
        outcome_value = ComposerPlannerAttemptOutcome(outcome)
        led_to_value = ComposerPlannerAttemptLedTo(led_to)
        planner_code_value = ComposerPlannerCode(planner_code) if planner_code is not None else None
        rejection_codes = _closed_planner_rejection_codes(codes)
        attempt = ComposerPlannerAttempt(
            ordinal=active.ordinal,
            planner_call_ordinal=active.planner_call_ordinal,
            phase=phase_value,
            outcome=outcome_value,
            planner_code=planner_code_value,
            selected_tools=active.selected_tools,
            requested_information=active.requested_information,
            new_information=new_information,
            rejection_codes=rejection_codes,
            candidate_shape_hash=active.candidate_shape_hash,
            repeated_fingerprint=repeated_fingerprint,
            led_to=led_to_value,
        )
        self._recorder.record_planner_attempt(attempt)
        self._active = None
        phase_name = phase_value.value
        if phase_name not in self.phase_counts:
            self.phase_counts[phase_name] = 0
        self.phase_counts[phase_name] += 1
        if rejection_codes:
            self.rejection_history.append({"attempt": attempt.ordinal, "outcome": outcome_value.value, "codes": list(rejection_codes)})
        slog.info(
            "composer.planner_attempt",
            session_id=self.session_id,
            operation_id=self.operation_id,
            surface=self.surface,
            attempt=attempt.ordinal,
            phase=phase_name,
            outcome=outcome_value.value,
            led_to=led_to_value.value,
            rejection_codes=list(rejection_codes),
            planner_code=planner_code_value.value if planner_code_value is not None else None,
            tool_calls=len(active.selected_tools),
            # True when this rejection's (component, code) fingerprint already
            # appeared in this request — the doctrine's "our bug" signal.
            repeated_fingerprint=repeated_fingerprint,
        )

    def finalize_active_exception(self, exc: BaseException) -> None:
        active = self._active
        if active is None:
            return
        if isinstance(exc, asyncio.CancelledError):
            outcome = ComposerPlannerAttemptOutcome.CANCELLED
            planner_code = None
        elif isinstance(exc, PipelinePlannerError):
            planner_code = exc.code
            if exc.code == ComposerPlannerCode.MALFORMED_RESPONSE.value:
                outcome = ComposerPlannerAttemptOutcome.MALFORMED_RESPONSE
            elif exc.code == ComposerPlannerCode.RESPONSE_TRUNCATED.value:
                outcome = ComposerPlannerAttemptOutcome.TRUNCATED
            elif exc.code in {
                ComposerPlannerCode.COMPLETION_TOKENS_EXCEEDED.value,
                ComposerPlannerCode.COMPOSITION_EXHAUSTED.value,
                ComposerPlannerCode.COST_CAP_EXCEEDED.value,
                ComposerPlannerCode.COST_UNAVAILABLE.value,
                ComposerPlannerCode.DISCOVERY_EXHAUSTED.value,
                ComposerPlannerCode.PROVIDER_CALLS_EXHAUSTED.value,
                ComposerPlannerCode.REPAIR_EXHAUSTED.value,
                ComposerPlannerCode.REQUEST_BYTES_EXHAUSTED.value,
                ComposerPlannerCode.TOOL_CALLS_EXHAUSTED.value,
            }:
                outcome = ComposerPlannerAttemptOutcome.BUDGET_EXHAUSTED
            elif exc.code == ComposerPlannerCode.DECLINED.value:
                outcome = ComposerPlannerAttemptOutcome.DECLINED
            else:
                outcome = ComposerPlannerAttemptOutcome.GUARD_FIRED
        else:
            outcome = ComposerPlannerAttemptOutcome.INTERNAL_ERROR
            planner_code = None
        self.finish_attempt(
            active.phase_hint,
            outcome,
            planner_code=planner_code,
            led_to=ComposerPlannerAttemptLedTo.TERMINAL,
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
    # Present exactly when custody finalization was deferred to the staging
    # settlement (elspeth-1e3ad83d89): the prepared inline source the
    # settlement must materialize atomically with the originating message.
    # In-memory only — never persisted or projected; ``custody_result`` stays
    # "ready" because the proposal cannot settle without the blob settling.
    custody_preparation: PipelineCustodyPreparation | None = None
    # The composition state this proposal WOULD produce, carried out so the
    # staging announce can measure a runtime-equivalent preflight against the
    # thing being proposed rather than against the (unmutated) current state
    # (elspeth-2ed41f0a4a). In-memory only — never persisted, hashed, or
    # projected; the draft hash covers the pipeline arguments, not this.
    #
    # Optional with a fail-closed default on purpose. A required field would
    # ripple to ~28 construction sites across the suite, and the surfaces that
    # do not set it (the in-loop set_pipeline proposal path in tool_batch.py)
    # do not publish the planner announce at all. Absent therefore means
    # "Stage 2 had nothing to measure", which the announce reports as
    # not-run rather than as validated.
    candidate_state: CompositionState | None = None

    def __post_init__(self) -> None:
        if type(self.proposal) is not PipelineProposal:
            raise TypeError("proposal must be an exact PipelineProposal")
        if self.candidate_state is not None and type(self.candidate_state) is not CompositionState:
            raise TypeError("candidate_state must be an exact CompositionState or None")
        if type(self.tool_call_id) is not str or not self.tool_call_id.strip():
            raise ValueError("tool_call_id must be a non-empty exact string")
        custody_result = cast(Any, self.custody_result)
        if type(custody_result) is not str or custody_result not in {"not_required", "ready"}:
            raise ValueError("custody_result must be 'not_required' or 'ready'")
        if self.custody_preparation is not None and type(self.custody_preparation) is not PipelineCustodyPreparation:
            raise TypeError("custody_preparation must be an exact PipelineCustodyPreparation or None")
        if self.custody_preparation is not None and custody_result != "ready":
            raise ValueError("custody_preparation requires custody_result 'ready'")
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


def planner_terminal_tool_definition(
    terminal_contract: PlannerTerminalContract | None = None,
) -> dict[str, Any]:
    """Return the sole terminal with the exact request-selected schema."""
    selected = terminal_contract or canonical_planner_terminal_contract()
    return {
        "type": "function",
        "function": {
            "name": _TERMINAL_TOOL_NAME,
            "description": "Return one complete canonical pipeline proposal for server validation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline": deep_thaw(selected.schema),
                    "claimed_deferred_intent_ids": _claimed_deferred_intent_schema(),
                },
                "required": ["pipeline"],
                "additionalProperties": False,
            },
        },
    }


def planner_tool_definitions(
    policy: PlannerDiscoveryPolicy | None = None,
    *,
    terminal_contract: PlannerTerminalContract | None = None,
) -> list[dict[str, Any]]:
    """Return an ordered request-owned read-only subset and sole terminal."""
    registered = {definition["name"]: definition for definition in get_tool_definitions()}
    missing = _PLANNER_DISCOVERY_TOOL_NAME_SET - registered.keys()
    if missing:
        raise RuntimeError(f"planner discovery declarations are missing: {sorted(missing)}")
    names = PLANNER_DISCOVERY_TOOL_NAMES if policy is None else policy.discovery_tool_names
    if tuple(name for name in PLANNER_DISCOVERY_TOOL_NAMES if name in names) != names:
        raise RuntimeError("planner discovery policy is not an order-preserving registered subset")
    discovery = [
        {
            "type": "function",
            "function": {
                "name": registered[name]["name"],
                "description": registered[name]["description"],
                "parameters": registered[name]["parameters"],
            },
        }
        for name in names
    ]
    return [*discovery, planner_terminal_tool_definition(terminal_contract)]


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


@observation_boundary(
    tier=3,
    source="a litellm/provider response object of unpinned vendor shape (dict, dataclass, or pydantic model)",
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns a field-name-to-value mapping merged from vars() and pydantic __pydantic_extra__ "
        "when derivable, or None when the value is not a Mapping and has no accessible __dict__; "
        "never raises (TypeError from vars() and AttributeError from the extras probe are both caught). "
        "The isinstance(extra, Mapping) site is reached only through object.__getattribute__(value, ...), "
        "a call that launders the suppression root, so it stays outside this decorator's suppressed scope "
        "by the analyzer's own model even though extra is semantically still value-derived"
    ),
)
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


# Protocol token an ordinary-turn text decline must lead with. The hatch turn
# accepts any text because its advisor is tool-restricted and taught by
# _escape_hatch_notice; an ordinary turn keeps the full palette, so text there
# is ambiguous between narration and decline and needs this mechanical
# discriminator, taught by _prose_decline_notice.
_PROSE_DECLINE_MARKER: Final[str] = "DECLINE:"


def _marked_decline_body(content: str, marker: str) -> str | None:
    """Return the decline body when content leads with marker, else None.

    Removing the protocol token (and its separating whitespace) is
    classification, not authorship: the returned body is the model's own
    words verbatim. A bare marker with no body is NOT a decline — there are
    no server-authored decline words, so an empty body routes to the nudge
    lane instead of a fallback. The marker is case-sensitive by design; the
    fail-safe direction is cheap (an uncased attempt costs one nudge).
    """
    stripped = content.lstrip()
    if not stripped.startswith(marker):
        return None
    body = stripped[len(marker) :].lstrip()
    return body if body else None


def _parse_response_tool_calls(
    response: Any,
    *,
    max_tool_calls: int,
    allow_text: bool = False,
    text_marker: str | None = None,
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
        if (
            allow_text
            and type(content) is str
            and content.strip()
            and (text_marker is None or _marked_decline_body(content, text_marker) is not None)
        ):
            try:
                require_bounded_text(content, label="planner text response")
            except JsonBoundaryError:
                # An over-bounds text reply cannot be admitted as a decline;
                # it stays in the no-tool-call class below so its treatment
                # matches every other unadmitted prose reply.
                pass
            else:
                return message, ()
        # The no-tool-call class (prose thinking-aloud, an empty reply,
        # marker-less text where a marker is required, or a text reply too
        # large to admit) gets its own code so the loop can nudge-retry it —
        # mid-plan prose is ordinary LLM behaviour, not provider breakage.
        # The loop converts it back to terminal MALFORMED_RESPONSE once the
        # nudge budget is spent; the code never escapes the planner.
        raise PipelinePlannerError("planner response must call a declared tool", code="PROSE_REPLY")
    if len(raw_calls) > max_tool_calls:
        raise PipelinePlannerError("planner response exceeds the per-turn tool call limit", code="MALFORMED_RESPONSE")
    parsed: list[_ParsedToolCall] = []
    seen_call_ids: set[str] = set()
    for raw_call in raw_calls:
        call_id = _provider_field(raw_call, "id")
        function = _provider_field(raw_call, "function")
        name = _provider_field(function, "name")
        raw_arguments = _provider_field(function, "arguments")
        if type(call_id) is not str or not call_id or type(name) is not str or not name:
            raise PipelinePlannerError("planner tool call metadata is malformed", code="MALFORMED_RESPONSE")
        if call_id in seen_call_ids:
            raise PipelinePlannerError("planner response contains duplicate tool call ids", code="MALFORMED_RESPONSE")
        seen_call_ids.add(call_id)
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

# Aggregate ceiling on every plugin contract this request hands the planner —
# the ones it asked for through get_plugin_schema AND the ones a rejection
# attaches for a violated plugin. One budget because the planner request is
# append-only: every selected contract is re-sent on every later turn, so what
# matters is the total the conversation carries, not any single payload.
_SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES: Final[int] = 48 * 1024

# Message prefix every ``bind_guided_reviewed_components`` complaint about the
# SHAPE OF THE CANDIDATE carries (guided/planning.py). Those describe what the
# planner authored, so they are repairable; every other AuditIntegrityError
# reaching the finalizer describes server-side authority and stays terminal.
_CANDIDATE_SHAPE_INTEGRITY_PREFIX: Final[str] = "guided planner candidate"

# The planner-surface terminal contract. The shared capability core is
# surface-neutral about terminal tools — its exact bytes also front the
# freeform tool loop, whose roster has no emit_pipeline_proposal
# (elspeth-3348db88f9) — so the exactly-once terminal instruction rides
# every planner request instead.
PLANNER_TERMINAL_INSTRUCTION: Final[str] = (
    "Use read-only discovery as needed, then call emit_pipeline_proposal exactly once "
    "with one complete canonical set_pipeline argument object."
)

DELTA_PLANNER_TERMINAL_INSTRUCTION: Final[str] = (
    "Use read-only discovery as needed, then call emit_pipeline_proposal exactly once. "
    "Set pipeline to exactly the mutable fields admitted by its advertised schema; "
    "do not emit omitted source, output, storage, failure-policy, or other reviewed authority. "
    "The server materializes those omitted fields into the canonical set_pipeline document."
)


def _prose_reply_notice() -> str:
    # Names the terminal tool: the generic "call a declared tool" wording was
    # satisfiable by any cheap discovery call — the live repro showed the
    # model answering each nudge with a discovery call and prosing again,
    # never reaching the terminal tool before the nudge budget spent.
    return (
        "Your previous reply called no tool. You must respond with a declared tool call — "
        "if your design is settled, call emit_pipeline_proposal with the complete proposal now; "
        "otherwise continue discovery from where you were."
    )


def _prose_decline_notice() -> str:
    # Static teaching text only: it names the decline format and nothing
    # else — no deployment, catalog, or policy facts — so it adds zero
    # provider egress. Appended once, on the first turn where a text decline
    # is legal.
    return (
        "If this request cannot be built with the capabilities available to this planning session, "
        f'reply in plain text starting with "{_PROSE_DECLINE_MARKER} " and state plainly, in the '
        "user's terms, what is missing. Use that prefix only for an honest decline; otherwise "
        "continue with tool calls."
    )


def _escape_hatch_notice() -> str:
    return (
        "The planning budget is exhausted; this is the escape hatch. You are a senior advisor model "
        "reviewing the retained transcript above as one freeform puzzle. Only protocol-complete turns are retained above; "
        "truncated responses and tool calls rejected before execution by discovery guards or discovery/composition budgets are omitted. "
        "You have exactly one turn: either "
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
_ROUTING_THRESHOLD_REVOCATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:remove|drop|delete|disable|omit|stop\s+using)\s+(?:the\s+)?(?:routing\s+)?(?:threshold|condition|gate|filter)\b"
    r"|\bwithout\s+(?:a\s+|the\s+)?(?:routing\s+)?(?:threshold|condition|gate|filter)\b"
    r"|\bno\s+(?:routing\s+)?(?:threshold|condition|gate|filter)\b"
    r"|\b(?:do\s+not|don(?:'|\N{RIGHT SINGLE QUOTATION MARK})t)\s+(?:use|apply|keep)\s+"
    r"(?:the\s+)?(?:routing\s+)?(?:threshold|condition|gate|filter)\b"
    r"|\b(?:do\s+not|don(?:'|\N{RIGHT SINGLE QUOTATION MARK})t)\s+(?:route|send|split|divert|separate)\b"
    r"|\b(?:fan(?:\s+out)?|route|send)\s+(?:every|all)\s+rows?\s+(?:to\s+)?(?:both|all|every)\s+(?:the\s+)?sinks?\b",
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
_ROUTING_CONTROL_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:routing\s+)?(?:threshold|gate|condition|filter)\b",
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


def _stated_threshold_for_planner_request(
    intent: str,
    conversation_context: PlannerConversationContext | None,
) -> str | None:
    """Resolve the latest authoritative routing threshold for this request.

    The current user message has precedence.  A referential freeform turn may
    otherwise inherit the latest retained earlier user request, while guided
    surfaces cannot provide ``conversation_context`` and therefore preserve
    their stage-local intent boundary.
    """
    if _ROUTING_THRESHOLD_REVOCATION_PATTERN.search(intent) is not None:
        return None
    current_threshold = _stated_threshold_in(intent)
    if current_threshold is not None:
        return current_threshold
    if _ROUTING_INTENT_PATTERN.search(intent) is not None or _ROUTING_CONTROL_REFERENCE_PATTERN.search(intent) is not None:
        return None
    if conversation_context is None:
        return None
    retained_requests = conversation_context.prior_user_requests
    if conversation_context.additional_prior_user_requests_omitted > 0:
        # The service retains the first request as a provider-visible anchor,
        # followed by recent tail entries. The omission gap between them may
        # hide a correction or revocation, so deterministic enforcement must
        # never scan across that gap back to the anchor.
        retained_requests = retained_requests[1:]
    for prior_request in reversed(retained_requests):
        if _ROUTING_THRESHOLD_REVOCATION_PATTERN.search(prior_request.content) is not None:
            return None
        prior_threshold = _stated_threshold_in(prior_request.content)
        if prior_threshold is not None:
            return prior_threshold
        # A newer routing instruction with no safely recognized comparison is
        # a supersession barrier. Searching past it would revive an older rule
        # that may have been replaced by an unconditional route or by syntax
        # the deliberately conservative detector cannot prove. A missed nudge
        # is safe; enforcing a stale threshold is not.
        if (
            _ROUTING_INTENT_PATTERN.search(prior_request.content) is not None
            or _ROUTING_CONTROL_REFERENCE_PATTERN.search(prior_request.content) is not None
        ):
            return None
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


def _candidate_policy_rejection(
    state: CompositionState,
    *,
    error_code: str,
) -> ToolResult:
    """Create one closed rejection raised by a post-validation surface policy."""

    if explain_validation_code(error_code) is None:
        raise AuditIntegrityError("candidate policy rejection code has no closed repair guidance")
    entry = ValidationEntry(
        component="pipeline",
        message="The candidate did not satisfy a surface-specific semantic obligation.",
        severity="high",
        error_code=error_code,
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
        _state_validation_withheld=True,
    )


def _rejection_entries(result: ToolResult) -> tuple[Any, ...]:
    """Return the entries the planner should actually repair against.

    A pre-application semantic rejection (``_failure_result``) leads with a
    ``rejected_mutation`` entry naming the real reason. Historically the
    UNCHANGED current state's ``state.validate()`` entries followed it — on
    the guided and tutorial surfaces that state is the empty seed, so every
    such rejection also carried ``no_source_configured`` +
    ``no_sinks_configured``, red herrings describing a state the planner is
    not editing (set_pipeline authors a full replacement). Tutorial session
    38e3e7f8 (op 1152d7e3, 2026-07-22) burned its repair budget on exactly
    that noise and "converged" by dropping every node. The set_pipeline
    producers now withhold those riders at the source (elspeth-e89e6bf47a);
    this gate stays as defense-in-depth for the planner surface. When
    rejection entries are present, they are the ONLY entries feedback and
    trail may carry; validated-candidate rejections (no ``rejected_mutation``
    entry) pass through untouched.
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
        "gate_on_error_unknown_sink",
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

    The component half must be the ATTRIBUTED ref, not the raw one: every
    pre-application entry is filed under the literal ``rejected_mutation``, so
    keying on it collapsed wholly disjoint component sets onto one
    fingerprint and told a planner its rejection set was "EXACTLY the same"
    when nothing about it was. This fingerprint is internal — it never leaves
    the loop — so the authored name inside a ref carries no egress.
    """
    return tuple(
        sorted(
            (_entry_component_ref(entry) or entry.component, entry.error_code or "validation_error") for entry in _rejection_entries(result)
        )
    )


_REPEAT_NOTICE = (
    "This candidate failed with EXACTLY the same rejection set (same components, same codes) "
    "as an earlier candidate in this request: the intervening changes did not fix — or "
    "reintroduced — the failure. Change ONLY the fields the errors below name, keep every "
    "other part of your last candidate byte-identical, and re-emit."
)

# Honest variant for a repeat whose facts are (partly) withheld: the ordinary
# notice's "change ONLY the fields the errors below name" is unsatisfiable when
# no field is named. Static text, never per-request data.
_REPEAT_NOTICE_WITHHELD = (
    "This candidate failed with EXACTLY the same rejection set (same components, same codes) "
    "as an earlier candidate in this request. At least one failing component is configured "
    "server-side for this surface and its validator detail is withheld, so re-emitting a "
    "near-identical candidate cannot succeed. Only a structurally different candidate — or an "
    "honest decline — can resolve this."
)

# Subject prefix of a ``rejected_mutation`` entry's message. These prefixes are
# authored by our own ``build_set_pipeline_candidate`` failure sites
# (``Source '<name>': …`` / ``Node '<id>': …`` / ``Output '<name>': …``), the
# same first-party format ``_INVALID_OPTIONS_PLUGIN_RE`` already parses for
# schema augmentation — Tier-1 parsing of ELSPETH-authored text, not a
# provider boundary.
_REJECTED_MUTATION_SUBJECT_RE: Final[re.Pattern[str]] = re.compile(r"^(Source|Node|Output) '([^']+)': ")


def _entry_component_ref(entry: Any) -> str | None:
    """Canonical validation-component ref a rejection entry is about.

    State-validation entries already carry the canonical vocabulary
    (``source`` / ``source:<name>`` / ``node:<id>`` / ``output:<name>`` /
    ``pipeline``). ``rejected_mutation`` entries name their subject in the
    message prefix instead; an unprefixed rejected_mutation message has no
    attributable subject and returns ``None`` (the withholding decision then
    fails closed whenever the finalizer owns anything).
    """
    component = entry.component
    if component != "rejected_mutation":
        return cast(str, component)
    match = _REJECTED_MUTATION_SUBJECT_RE.match(entry.message)
    if match is None:
        return None
    kind, name = match.groups()
    if kind == "Node":
        return f"node:{name}"
    if kind == "Source":
        return "source" if name == "source" else f"source:{name}"
    return f"output:{name}"


# Codes whose projection attaches instance ``connectivity`` facts — the only
# projection that quotes a component's ROUTING values, so it is additionally
# suppressed for routing-owned components.
_CONNECTIVITY_FACT_CODES: Final[frozenset[str]] = _ROUTE_DESTINATION_FACT_CODES | {"coalesce_branch_unreachable"}


@dataclass(frozen=True, slots=True)
class _EntryWithholding:
    """Per-entry withholding decision (elspeth-5904b1683a).

    ``config`` masks the entry to component ``"pipeline"``, swaps in the
    honest blind-mode guidance, and strips validator ``detail`` and every
    structured candidate fact — the entry is about a component whose
    configuration the finalizer wrote, so any of those could quote reviewed
    private values. ``connectivity`` strips only the connectivity facts — the
    component's options (and so its ``detail``) are still exactly what the
    model authored, but its routing destinations were finalizer-written.
    ``contract`` and ``row_union`` strip their structured facts when the fact
    PAYLOAD derives from a config-owned component even though the entry
    itself is attributed to a model-authored consumer — contract
    ``extra_fields`` are computed from the PRODUCER's live guarantees, and
    row_union branch declarations are read from upstream connections'
    schemas, so entry-own attribution alone would leak a bound private
    source's real field names. ``withheld`` is the honesty signal: True when
    this entry was actually projected with something suppressed, feeding the
    withheld repeat notice and the repeat-while-blind short-circuit.
    """

    config: bool
    connectivity: bool
    contract: bool
    row_union: bool
    coalesce_union_type: bool
    withheld: bool


_ENTRY_DISCLOSED: Final[_EntryWithholding] = _EntryWithholding(
    config=False, connectivity=False, contract=False, row_union=False, coalesce_union_type=False, withheld=False
)


def _contract_participant_refs(contract: Any) -> tuple[str, ...]:
    """Validation-component refs of a contract fact's producer and consumer.

    ``SchemaContractDetail`` carries producer/consumer in the producer-id
    vocabulary: ``source`` / ``source:<name>`` for sources, ``output:<name>``
    for sinks, and BARE node ids for nodes (see ``_producer_owner`` in
    ``composer.state``) — normalize the bare form so the ownership check
    compares like with like.
    """
    refs: list[str] = []
    for participant in (contract.producer, contract.consumer):
        if type(participant) is not str or not participant:
            continue
        if participant == "source" or participant.startswith(("source:", "output:", "node:")):
            refs.append(participant)
        else:
            refs.append(f"node:{participant}")
    return tuple(refs)


def _entry_withholding(entry: Any, finalizer_owned: _FinalizerOwnedRefs) -> _EntryWithholding:
    """Withholding decision for one rejection entry.

    Entry-scoped custody (elspeth-5904b1683a): withhold exactly the entries
    about components the candidate finalizer owns, per change kind (see
    :class:`_FinalizerOwnedRefs`). Entries whose subject cannot be attributed
    fail closed while any server ownership exists, as do fact payloads whose
    content derives from another component that is config-owned (contract
    producer/consumer cross-refs) or cannot be attributed at all (row_union
    branch schemas, which are read from upstream connections' declarations
    the detail does not name — suppressed whenever any config ownership
    exists). Entries on components the model authored — untouched by the
    finalizer — disclose nothing the model does not already hold.
    """
    if not finalizer_owned.owns_anything():
        return _ENTRY_DISCLOSED
    ref = _entry_component_ref(entry)
    config = ref is None or ref in finalizer_owned.config
    connectivity = config or ref in finalizer_owned.routing
    contract = config or (
        entry.contract is not None
        and any(participant in finalizer_owned.config for participant in _contract_participant_refs(entry.contract))
    )
    row_union = config or (entry.row_union_schema is not None and bool(finalizer_owned.config))
    # Same payload provenance as ``row_union``: a coalesce's branch types are
    # read from upstream connections' declarations the detail does not name, so
    # entry-own attribution cannot prove the types did not come from a bound
    # private source. Suppress whenever any config ownership exists.
    coalesce_union_type = config or (entry.coalesce_union_type is not None and bool(finalizer_owned.config))
    code = entry.error_code or "validation_error"
    withheld = (
        config
        or (connectivity and code in _CONNECTIVITY_FACT_CODES)
        or (contract and entry.contract is not None)
        or (row_union and entry.row_union_schema is not None)
        or (coalesce_union_type and entry.coalesce_union_type is not None)
    )
    return _EntryWithholding(
        config=config,
        connectivity=connectivity,
        contract=contract,
        row_union=row_union,
        coalesce_union_type=coalesce_union_type,
        withheld=withheld,
    )


def _rejection_facts_withheld(result: ToolResult, finalizer_owned: _FinalizerOwnedRefs) -> bool:
    """Whether ANY entry of one rejection was projected in withheld form."""
    return any(_entry_withholding(entry, finalizer_owned).withheld for entry in _rejection_entries(result))


def _withheld_component_count(result: ToolResult) -> int:
    """How many defective components the candidate builder counted but did not list."""
    data = result.data
    if not isinstance(data, Mapping) or COMPONENTS_WITHHELD_KEY not in data:
        return 0
    withheld = data[COMPONENTS_WITHHELD_KEY]
    return withheld if type(withheld) is int else 0


# Static usage line, never per-request data. Live planners called
# explain_validation_error with junk ({"error_text": "ValidationError"})
# because nothing said the exact code string is the lookup key. Kept
# deliberately free of topology hints — mid-repair suggestions have derailed
# otherwise-converging repairs.
_EXPLAIN_VALIDATION_ERROR_GUIDANCE: Final[str] = "To expand any code, call explain_validation_error with the exact code string."


def _explain_tool_advertisement_earns_its_turn(errors: Sequence[Mapping[str, Any]]) -> bool:
    """Whether pointing at ``explain_validation_error`` can still tell the model anything.

    The tool and this projection read the SAME closed catalogue, so for an
    entry that already carries its inline ``(explanation, suggested_fix)``
    the call returns byte-equivalent guidance — a whole provider turn spent
    to re-read text already in the context (elspeth-41b406c9fc). Advertise it
    only while some entry arrived without that enrichment, which is exactly
    when the call can add something; in withheld mode every entry is enriched
    by construction, so the guaranteed-empty call is never suggested.
    """
    return any("explanation" not in entry for entry in errors)


def _allowlisted_candidate_feedback(
    result: ToolResult,
    *,
    repeated_fingerprint: bool = False,
    finalizer_owned: _FinalizerOwnedRefs = _FINALIZER_OWNS_NOTHING,
    components_withheld: int = 0,
    plugin_contract_resolver: Callable[[PluginKind, str], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
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

    ``finalizer_owned`` scopes that custody judgment PER ENTRY and PER
    CHANGE KIND (elspeth-5904b1683a). Entries about config-owned components
    (guided reviewed sources/outputs, correction-restored nodes, auto-wired
    controls) are masked to component ``"pipeline"`` and stripped of detail
    and every instance fact, because their validator messages can quote
    reviewed private values redacted from the provider context. Entries
    about routing-owned components keep their true component id and detail
    — their options are exactly what the model authored — but lose only
    their ``connectivity`` facts, the one projection that quotes
    finalizer-written routing destinations. The predecessor candidate-global
    predicate (any finalizer mutation withholds every entry) made guided
    repair permanently blind — the guided binder ALWAYS mutates the
    candidate — and drove deterministic REPAIR_EXHAUSTED on any
    first-candidate option mistake. Cross-component identifiers inside kept
    facts (``declared_sinks``, contract producer/consumer ids) are
    structural labels, never option values, and remain the repair
    vocabulary the model must use.

    ``plugin_contract_resolver`` attaches the projected planner contract for
    every plugin a ``plugin_options_invalid`` entry names, under the SAME
    entry-scoped withholding as ``detail`` — a config-owned component's
    validator message is withheld, and so is the plugin identity inside it.
    The freeform surface has done this since the option-shape augmentation
    landed (``build_plugin_schemas_for_failure``); the planner surface was
    denied it and had to buy the same bytes with a ``get_plugin_schema`` turn.
    The contract is rehydrated through the request's live policy view at
    synthesis time — never stored bytes — so a plugin the policy view no
    longer exposes cannot be resurrected from an earlier turn, and it is
    charged against the same aggregate contract budget the model-requested
    schemas draw on. Charging it HERE is what makes a violated plugin's
    contract win the budget over a later speculative ``get_plugin_schema``;
    nothing already sent is ever evicted to make room.
    """
    validation = result.validation
    errors: list[dict[str, Any]] = []
    reachability_facts: dict[str, dict[str, Any]] | None = None
    destination_facts: dict[str, RouteDestinationFactDict] | None = None
    any_facts_withheld = False
    for entry in _rejection_entries(result):
        code = entry.error_code or "validation_error"
        withholding = _entry_withholding(entry, finalizer_owned)
        withhold_candidate_facts = withholding.config
        any_facts_withheld = any_facts_withheld or withholding.withheld
        # A pre-application rejection entry is filed under the literal
        # component ``rejected_mutation`` and names its subject only in the
        # message prefix — which this projection withholds. One rejection can
        # now carry several such entries (elspeth-4fad98a453), so projecting
        # the raw component would give the model N indistinguishable entries.
        # ``_entry_component_ref`` resolves the prefix into the same canonical
        # vocabulary state-validation entries already use; the name inside it
        # is one the planner authored in the candidate being answered.
        attributed_component = _entry_component_ref(entry)
        component = "pipeline" if withhold_candidate_facts else entry.component if attributed_component is None else attributed_component
        projected: dict[str, Any] = {
            "component": component,
            "severity": entry.severity,
            "error_code": code,
            "error_class": "ValidationError",
        }
        guidance = explain_withheld_validation_code(code) if withhold_candidate_facts else explain_validation_code(code)
        if guidance is not None:
            projected["explanation"], projected["suggested_fix"] = guidance
        if (
            code
            in (
                "plugin_options_invalid",
                "gate_condition_ignores_stated_threshold",
                _UNPRODUCIBLE_OUTPUT_FIELDS_CODE,
            )
            and not withhold_candidate_facts
        ):
            # Same custody judgment for all three: the message quotes only
            # content the planner itself already holds verbatim — the options
            # of the candidate it just authored, the comparison span from the
            # instruction its own prompt was built from, or the reviewed output
            # field names already in its ``reviewed_planner_context``.
            projected["detail"] = entry.message
        if code == "plugin_options_invalid" and not withhold_candidate_facts and plugin_contract_resolver is not None:
            # The detail above names only the VIOLATED keys; the contract names
            # every key the plugin accepts and how. Same custody class as the
            # detail it rides with — a public plugin schema this provider can
            # already read with get_plugin_schema on this surface — so the only
            # thing attaching it changes is which turn the planner learns it on.
            #
            # The identity comes from the PRODUCER, never from the message.
            # These messages interpolate model-authored text in three separate
            # places — the rejected option values, the secret_ref-placement
            # head's option keys, and the component-name attribution prefix,
            # which is unvalidated for exactly the components that fail — and
            # each one was enough to plant a plugin identity that no validator
            # had admitted. Resolving one of those raises out of this synthesis
            # and kills the whole request where a repair turn was correct.
            # ``ValidationEntry.plugin_identity`` is recorded where the failure
            # is built, from what the validator actually resolved.
            #
            # No parse fallback: an entry without the carrier attaches nothing.
            # A fallback would reopen every one of those vectors for precisely
            # the entries that lack it.
            contract = None
            if entry.plugin_identity is not None:
                contract = plugin_contract_resolver(*entry.plugin_identity)
            if contract is not None:
                # A list because attribution belongs to the entry, not the
                # message: every collected entry carries its own subject, and
                # this key is where a future producer attributing two would go.
                projected["plugin_contracts"] = [contract]
        if code in _ROUTE_DESTINATION_FACT_CODES and not withholding.connectivity:
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
        if code == "coalesce_branch_unreachable" and not withholding.connectivity:
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
        if entry.contract is not None and not withholding.contract:
            # Structured contract facts: producer/consumer component ids and
            # schema FIELD NAMES from validated contract config — pipeline
            # metadata the session owner authored, never user row content
            # (see SchemaContractDetail in composer.state). Without them a
            # schema-contract rejection is a bare code the planner cannot
            # repair within budget: it must know WHICH edge failed and WHICH
            # fields are missing. This stays inside the message-redaction
            # boundary this allowlist protects — but ONLY when neither
            # participant is finalizer-config-owned: ``extra_fields`` are
            # computed from the PRODUCER's live guarantees, so a contract
            # entry attributed to a model-authored consumer can still carry a
            # bound private source's real field names
            # (``withholding.contract``, elspeth-5904b1683a review finding).
            projected["contract"] = entry.contract.to_dict()
        if entry.row_union_schema is not None and not withholding.row_union:
            # Structured row-union branch declarations: branch aliases,
            # schema modes, field names, and declared field properties from
            # the REJECTED candidate the planner authored. These are the
            # row-union equivalent of the safe contract facts above, never
            # runtime row content, and make the incompatibility repairable
            # without exposing the free-form validation message. Branch
            # schemas are read from upstream connections' declarations the
            # detail does NOT name, so they cannot be attributed per
            # participant — suppressed whenever any config ownership exists
            # (``withholding.row_union``, elspeth-5904b1683a; precise branch
            # attribution is tracked as follow-up work).
            projected["row_union_schema"] = entry.row_union_schema.to_dict()
        if entry.coalesce_union_type is not None and not withholding.coalesce_union_type:
            # The conflicting field, the two branch names, and their declared
            # types — the coalesce equivalent of the row_union facts above.
            # Without them the closed code names the failing NODE but never the
            # FIELD, and the static guidance ("declare the same type on every
            # branch that declares it") is unsatisfiable in the case where a
            # branch conflicts on a field it never declared, because a plugin
            # contributed the field as a computed output (elspeth-85f3cc3022).
            projected["coalesce_union_type"] = entry.coalesce_union_type.to_dict()
        errors.append(projected)
    feedback: dict[str, Any] = {
        "success": False,
        "validation": {
            "is_valid": validation.is_valid,
            "errors": errors,
        },
    }
    if _explain_tool_advertisement_earns_its_turn(errors):
        feedback["guidance"] = _EXPLAIN_VALIDATION_ERROR_GUIDANCE
    if components_withheld:
        # Never silent: the candidate builder caps how many defective
        # components one rejection lists, and the model must be able to tell
        # "these are all of them" from "these are the first of them".
        feedback["truncation_notice"] = (
            f"{components_withheld} further component(s) of this candidate also failed validation and are not "
            "listed here. Repair every component named above and re-emit; the remaining failures are reported "
            "on the next turn."
        )
    if repeated_fingerprint:
        # Static text, never per-request data: names the repetition the model
        # cannot see on its own (it has no attempt counter) so budget stops
        # burning on byte-identical failures without the model knowing. The
        # withheld variant is honest about WHY re-emitting cannot succeed —
        # the ordinary notice's "change ONLY the fields the errors below
        # name" names no field when facts are withheld.
        feedback["repeat_notice"] = _REPEAT_NOTICE_WITHHELD if any_facts_withheld else _REPEAT_NOTICE
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


def _binding_rejection_feedback(
    rejection: GuidedCandidateBindingRejected,
    *,
    repeated_fingerprint: bool,
) -> Mapping[str, Any]:
    """Project one typed binder rejection into closed repair feedback.

    Mirrors ``_allowlisted_candidate_feedback``'s entry shape: the closed
    code, the catalogue ``(explanation, suggested_fix)`` when registered, and
    the rejection's own ``connectivity`` facts — which the binder already
    restricted to planner-authored strings and reviewed sink names, the same
    custody class as ``route_destination_facts`` (elspeth-572c642dbf). No raw
    exception message crosses: the entry is built solely from the closed code
    and the structured facts.
    """
    entry: dict[str, Any] = {
        "component": "pipeline",
        "severity": "high",
        "error_code": rejection.error_code,
        "error_class": "ValidationError",
    }
    guidance = explain_validation_code(rejection.error_code)
    if guidance is not None:
        entry["explanation"], entry["suggested_fix"] = guidance
    if rejection.connectivity:
        entry["connectivity"] = dict(rejection.connectivity)
    feedback: dict[str, Any] = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [entry],
        },
    }
    if _explain_tool_advertisement_earns_its_turn([entry]):
        feedback["guidance"] = _EXPLAIN_VALIDATION_ERROR_GUIDANCE
    if repeated_fingerprint:
        feedback["repeat_notice"] = _REPEAT_NOTICE
    return feedback


def _binding_rejection_fingerprint(rejection: GuidedCandidateBindingRejected) -> tuple[tuple[str, str], ...]:
    """Identity of one binder rejection: its code plus what the code is ABOUT.

    The code alone is far too coarse. Ten binder sites share
    ``guided_delta_authority_violation``, so a candidate that fixed a wrong
    source name and then tripped a missing ``on_success`` drew the repeat
    notice — which asserts the rejection set is EXACTLY the same and tells
    the model to keep every other part byte-identical. That is false, and it
    can steer a planner into reverting a genuine fix. The rejection's own
    connectivity facts carry the discriminators: which collection the
    complaint is about, and which delta member the binder was reading. Both
    are closed structural labels the binder authors, never candidate values,
    so a genuine repeat — same code, same facts — still fingerprints the same
    and still draws the notice.
    """
    facts = rejection.connectivity
    discriminators: list[tuple[str, str]] = []
    for key in ("component_kind", "delta_member"):
        if key not in facts:
            continue
        fact = facts[key]
        if type(fact) is str:
            discriminators.append((key, fact))
    return (("pipeline", rejection.error_code), *discriminators)


# How many schema violations one canonical-schema rejection names. A bare
# ``canonical_schema`` code costs a whole repair turn to localize
# (elspeth-4fad98a453), but an unbounded list of a pathological payload's
# every violation is its own wall; the withheld count keeps truncation
# visible.
_MAX_REPORTED_SCHEMA_VIOLATIONS: Final[int] = 8


class _SchemaViolation(TypedDict):
    """One structural violation, as location + rule with no rejected value."""

    path: str
    rule: str
    constraint: NotRequired[str | int]
    detail: NotRequired[str]


def _schema_violation_path(parts: Sequence[Any]) -> str:
    """Name a violation's location inside the candidate the planner authored."""
    return "/".join(str(part) for part in parts) if parts else "pipeline"


def _schema_violation_component_ref(parts: Sequence[Any]) -> str | None:
    """Canonical component ref a violation path names, or ``None``.

    Only the two source shapes carry a component's NAME in the path itself
    (``source/…`` and ``sources/<name>/…``, the same vocabulary
    ``_candidate_component_blocks`` produces). ``nodes`` / ``outputs`` locate
    by list INDEX and the remaining top-level members name no component at
    all, so those paths are unattributable by construction — which the custody
    rule reads as fail-closed, exactly as ``_entry_withholding`` does for an
    unattributable rejection entry.
    """
    if not parts:
        return None
    if parts[0] == "source":
        return "source"
    if parts[0] == "sources" and len(parts) > 1 and type(parts[1]) is str and parts[1]:
        return "source" if parts[1] == "source" else f"source:{parts[1]}"
    return None


def _structural_schema_violations(errors: Iterable[Any]) -> tuple[list[_SchemaViolation], int]:
    """Project JSON-Schema errors into located, value-free repair facts.

    The pre-check runs against the payload the planner itself authored, and
    the schema is the one already advertised to it on this surface: the JSON
    path, the violated keyword, and a scalar constraint are all facts the
    same provider holds, so naming them discloses nothing new. The error's
    own ``message`` is NOT projected — jsonschema embeds the rejected
    instance in it, and a rejected value is the one thing this projection
    must never echo back into an audited transcript.
    """
    violations: list[_SchemaViolation] = []
    withheld = 0
    for error in errors:
        if len(violations) >= _MAX_REPORTED_SCHEMA_VIOLATIONS:
            withheld += 1
            continue
        violation: _SchemaViolation = {
            "path": _schema_violation_path(list(error.absolute_path)),
            "rule": str(error.validator),
        }
        constraint = error.validator_value
        if type(constraint) is str or type(constraint) is int:
            violation["constraint"] = constraint
        violations.append(violation)
    return violations, withheld


def _pydantic_schema_violations(
    exc: PydanticValidationError,
    *,
    finalizer_owned: _FinalizerOwnedRefs,
) -> tuple[list[_SchemaViolation], int]:
    """Project argument-model errors into located repair facts, under custody.

    Runs on the MATERIALIZED candidate, so a location can name a component
    the server bound rather than one the planner wrote: ``sources`` is a
    mapping, and on a guided surface the finalizer keys it by a REVIEWED
    source's user-given name. Entry-scoped custody therefore applies here
    exactly as it does to a rejection entry (elspeth-5904b1683a) — a
    violation about a config-owned component, or one whose path attributes to
    no component at all, is reported as an unlocated rule. The mask also
    drops ``detail``: pydantic's length family renders a count derived from
    the rejected input ("…not 5"), and a derived measure of private
    configuration is disclosure.

    Where custody permits the location, pydantic's closed error ``type`` and
    the human ``msg`` ride with it — for pydantic's own built-in types the
    text is a value-free template. ``value_error`` / ``assertion_error``
    messages are written by a validator and can quote whatever it was handed,
    so those two arms keep the location and drop the text.
    """
    server_owns_something = finalizer_owned.owns_anything()
    violations: list[_SchemaViolation] = []
    withheld = 0
    for error in exc.errors():
        if len(violations) >= _MAX_REPORTED_SCHEMA_VIOLATIONS:
            withheld += 1
            continue
        rule = str(error["type"])
        parts = list(error["loc"])
        ref = _schema_violation_component_ref(parts)
        if server_owns_something and (ref is None or ref in finalizer_owned.config):
            violations.append({"path": "pipeline", "rule": rule})
            continue
        violation: _SchemaViolation = {
            "path": _schema_violation_path(parts),
            "rule": rule,
        }
        if rule not in {"value_error", "assertion_error"}:
            violation["detail"] = str(error["msg"])
        violations.append(violation)
    return violations, withheld


def _log_schema_precheck_rejection(trail: _PlannerAttemptTrail, errors: Sequence[Any]) -> None:
    """Emit the opted-in diagnostic for a candidate stopped at the schema pre-check.

    The Stage-1 emission (below, in the candidate-rejected arm) never saw
    these candidates: the pre-check answers them before any tool runs, so an
    operator debugging a repair loop lost the whole class. Same seam, same
    rule — closed classifiers only. The INSTANCE path is deliberately not
    logged: a mapping key inside it is authored text (a source name, a route
    label). The schema path is our own advertised schema's structure and the
    validator keyword is a JSON Schema keyword, so neither can carry an
    authored value.
    """
    if os.environ.get("ELSPETH_PLANNER_REJECTION_DETAIL_LOG") != "1":
        return
    slog.warning(
        "composer.planner_rejection_detail",
        session_id=trail.session_id,
        operation_id=trail.operation_id,
        attempt=trail.attempts,
        entries=[
            {
                "component": "pipeline",
                "error_code": "canonical_schema",
                "severity": "high",
                "rule": str(error.validator),
                "schema_path": "/".join(str(part) for part in error.absolute_schema_path),
            }
            for error in errors[:_MAX_REPORTED_SCHEMA_VIOLATIONS]
        ],
    )


def _canonical_schema_feedback(
    violations: Sequence[_SchemaViolation] = (),
    *,
    violations_withheld: int = 0,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "component": "pipeline",
        "severity": "high",
        "error_code": "canonical_schema",
        "error_class": "SchemaValidationError",
    }
    if violations:
        entry["schema_violations"] = list(violations)
        if violations_withheld:
            entry["schema_violations_withheld"] = violations_withheld
    return {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [entry],
        },
    }


type _TerminalMaterializationOutcome = tuple[dict[str, Any] | None, _FinalizerOwnedRefs, Mapping[str, Any] | None, bool]


def _materialize_terminal_payload(
    *,
    payload: Mapping[str, Any],
    terminal_contract: PlannerTerminalContract,
    seen_rejection_fingerprints: set[tuple[tuple[str, str], ...]],
) -> _TerminalMaterializationOutcome:
    """Expand an admitted provider payload into a canonical owned pipeline."""
    owned_refs = _FINALIZER_OWNS_NOTHING
    try:
        materialized = terminal_contract.materialize(payload)
        if type(materialized) is PlannerTerminalMaterialization:
            pipeline_result = deep_thaw(materialized.pipeline)
            owned_refs = _FinalizerOwnedRefs(
                config=materialized.config_owned_refs,
                routing=materialized.routing_owned_refs,
            )
        else:
            pipeline_result = materialized
        if type(pipeline_result) is not dict:
            raise AuditIntegrityError("planner terminal materializer must return an exact dict")
        SetPipelineArgumentsModel.model_validate(pipeline_result)
    except GuidedCandidateBindingRejected as exc:
        binding_fingerprint = _binding_rejection_fingerprint(exc)
        repeated = binding_fingerprint in seen_rejection_fingerprints
        seen_rejection_fingerprints.add(binding_fingerprint)
        return (
            None,
            owned_refs,
            _binding_rejection_feedback(exc, repeated_fingerprint=repeated),
            repeated,
        )
    except PydanticValidationError as exc:
        # Locate every argument-model defect rather than answering with a bare
        # code: the materialized candidate can be large, and "it does not match
        # the canonical schema" told the planner nothing about WHERE
        # (elspeth-4fad98a453). The custody refs are passed HERE rather than
        # applied inside ``_canonical_schema_feedback``: that builder also
        # serves the pre-check arm, whose instance paths are planner-authored
        # by construction (it runs before materialization), and a mask there
        # would degrade those back toward the bare code.
        violations, violations_withheld = _pydantic_schema_violations(exc, finalizer_owned=owned_refs)
        return (
            None,
            owned_refs,
            _canonical_schema_feedback(violations, violations_withheld=violations_withheld),
            False,
        )
    return pipeline_result, owned_refs, None, False


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


def _project_planner_plugin_contract(data: object) -> tuple[PlannerPluginContract | None, bool]:
    """Parse unowned schema data into one owned planner contract outcome."""
    if type(data) is not PluginSchemaInfo:
        return None, False
    try:
        contract = planner_plugin_contract(data)
    except SchemaContractProjectionUnsupported:
        return None, False
    return contract, True


@observation_boundary(
    tier=3,
    source="ToolResult.data from executing a model-requested discovery tool call (unpinned payload shape)",
    source_param="result",
    suppresses=("R5",),
    invariant=(
        "dispatches on result.data's shape (an authoritative-state component echo) and falls closed "
        "to the leak-safe surface_projection_unavailable payload for anything unrecognized; never "
        "raises. Two of this function's isinstance sites root at provider_current_state (a separate, "
        "policy-owned server-computed projection, not this boundary) and are outside this decorator's "
        "scope by design"
    ),
)
def _serialize_provider_discovery_result(
    *,
    call: _ParsedToolCall,
    result: ToolResult,
    surface: PlannerSurface,
    provider_current_state: Mapping[str, Any],
    schema_contract_budget_remaining: int | None = None,
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
    restricted = surface in {PlannerSurface.GUIDED_STAGED, PlannerSurface.TUTORIAL_PROFILE}
    if call.name == "get_plugin_schema" and result.success:
        contract, projection_available = _project_planner_plugin_contract(result.data)
        if not projection_available:
            closed = _closed_provider_discovery_payload(result)
            closed["success"] = False
            closed["data"] = {
                "error": "The selected plugin schema cannot be represented in the bounded planner projection. Use get_plugin_assistance.",
                "error_code": "schema_projection_unavailable",
                "next_tool": "get_plugin_assistance",
            }
            return _serialize_closed_provider_discovery_payload(closed)
        assert contract is not None
        contract_payload = contract.to_dict()
        if (
            schema_contract_budget_remaining is not None
            and len(canonical_json(contract_payload).encode("utf-8")) > schema_contract_budget_remaining
        ):
            closed = _closed_provider_discovery_payload(result)
            closed["success"] = False
            closed["data"] = {
                "error": "The selected plugin contracts exceed the aggregate planner schema budget. Use get_plugin_assistance.",
                "error_code": "schema_contract_budget_exceeded",
                "next_tool": "get_plugin_assistance",
            }
            return _serialize_closed_provider_discovery_payload(closed)
        if restricted:
            closed = _closed_provider_discovery_payload(result)
            closed["data"] = contract_payload
            return _serialize_closed_provider_discovery_payload(closed)
        return serialize_tool_result(replace(result, data=contract_payload))
    if not restricted:
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


def _attach_planner_evidence(
    exc: BaseException,
    recorder: BufferingRecorder,
    *,
    llm_call_start: int,
    planner_attempt_start: int,
) -> None:
    """Attach both halves of this planner operation's paired audit evidence."""
    attach_llm_calls(exc, recorder, start_index=llm_call_start)
    exc_with_evidence = cast(Any, exc)
    exc_with_evidence.planner_attempts = recorder.planner_attempts[planner_attempt_start:]


async def _build_valid_pipeline_plan(
    *,
    pipeline: Mapping[str, Any],
    current_state: CompositionState,
    base: ProposalBase,
    reviewed_facts: Mapping[str, Any],
    claimed_deferred_intent_ids: tuple[str, ...],
    claim_evaluator: PipelineClaimEvaluator | None,
    candidate_acceptance: PipelineCandidateAcceptance | None,
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
        claim_evaluator(candidate.result.updated_state, claimed_deferred_intent_ids) if claim_evaluator is not None else ()
    )
    if claimed_deferred_intent_ids and claim_evaluator is None:
        raise DeferredIntentClaimError("this planner surface has no eligible deferred intent claims")
    if type(covered_deferred_intent_ids) is not tuple or any(type(intent_id) is not str for intent_id in covered_deferred_intent_ids):
        raise AuditIntegrityError("deferred intent claim evaluator returned malformed coverage")
    if len(set(covered_deferred_intent_ids)) != len(covered_deferred_intent_ids) or set(covered_deferred_intent_ids) != set(
        claimed_deferred_intent_ids
    ):
        raise AuditIntegrityError("deferred intent claim evaluator changed the claimed identity set")
    if candidate_acceptance is not None:
        try:
            candidate_acceptance(candidate.result.updated_state)
        except PipelineCandidatePolicyRejection as exc:
            raise _PipelineCandidateRejected(
                _candidate_policy_rejection(
                    candidate.result.updated_state,
                    error_code=exc.error_code,
                )
            ) from exc

    safe_pipeline: Mapping[str, Any] = pipeline
    # The candidate state that corresponds to ``safe_pipeline``. Custody
    # rewriting below replaces BOTH together — carrying the pre-custody state
    # alongside a custody-rewritten pipeline would hand the staging announce a
    # Stage-2 verdict for a pipeline that is not the one being proposed.
    candidate_state = candidate.result.updated_state
    custody_result: PipelineCustodyResult = "not_required"
    custody_preparation: PipelineCustodyPreparation | None = None
    if candidate.prepared_inline_blob is not None:
        if custody_config.session_engine is None:
            raise AuditIntegrityError("inline pipeline custody requires session_engine")
        preparation = prepare_pipeline_custody(
            pipeline,
            candidate.prepared_inline_blob,
            session_id=originating_message.session_id,
            max_storage_per_session=custody_config.max_storage_per_session,
        )
        pending_custody_view: PendingCustodyBlobView | None = None
        if custody_config.defer_finalize:
            # The blob row's lineage FK needs the originating chat message,
            # which this surface inserts only inside the atomic staging
            # settlement — carry the preparation there instead of violating
            # the FK here (elspeth-1e3ad83d89). The revalidation below must
            # still resolve the rewritten blob_id, so hand it the
            # settlement-equivalent view of this one blob
            # (elspeth-282f392fae).
            custody_preparation = preparation
            pending_custody_view = pending_custody_blob_view(
                preparation,
                data_dir=custody_config.data_dir,
            )
        else:
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
            _pending_custody=pending_custody_view,
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
        candidate_state = safe_candidate.result.updated_state
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
        custody_preparation=custody_preparation,
        candidate_state=candidate_state,
    )


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
    conversation_context: PlannerConversationContext | None = None,
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
    candidate_acceptance: PipelineCandidateAcceptance | None = None,
    terminal_contract: PlannerTerminalContract | None = None,
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
    if conversation_context is not None and type(conversation_context) is not PlannerConversationContext:
        raise TypeError("conversation_context must be an exact PlannerConversationContext or None")
    if conversation_context is not None and surface is not PlannerSurface.FREEFORM:
        raise ValueError("conversation_context is available only to the freeform planner surface")
    if policy_catalog.snapshot is not plugin_snapshot:
        raise ValueError("plugin_snapshot_catalog_mismatch")
    canonical_json(provider_current_state)
    if not callable(candidate_finalizer):
        raise TypeError("candidate_finalizer must be callable")
    if candidate_acceptance is not None and not callable(candidate_acceptance):
        raise TypeError("candidate_acceptance must be callable or None")
    if terminal_contract is not None and type(terminal_contract) is not PlannerTerminalContract:
        raise TypeError("terminal_contract must be an exact PlannerTerminalContract or None")
    selected_terminal_contract = terminal_contract or canonical_planner_terminal_contract()
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
    planner_attempt_start = len(recorder.planner_attempts)
    outcome: PlannerSettlement = "failed"
    primary_error: BaseException | None = None
    # The guided write fence carries the operation identity; freeform has
    # none. Reused here so the trail correlates with the durable operation
    # rows without widening the planner signature.
    trail = _PlannerAttemptTrail(
        session_id=originating_message.session_id,
        operation_id=(custody_config.write_fence.operation_id if custody_config.write_fence is not None else None),
        surface=surface.value,
        recorder=recorder,
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
                conversation_context=conversation_context,
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
                candidate_acceptance=candidate_acceptance,
                terminal_contract=selected_terminal_contract,
            )
        outcome = "complete"
        trail.log_summary("accepted")
        return proposal
    except BaseException as exc:
        primary_error = exc
        trail.finalize_active_exception(exc)
        _attach_planner_evidence(
            exc,
            recorder,
            llm_call_start=llm_call_start,
            planner_attempt_start=planner_attempt_start,
        )
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
                _attach_planner_evidence(
                    settlement_error,
                    recorder,
                    llm_call_start=llm_call_start,
                    planner_attempt_start=planner_attempt_start,
                )
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
    conversation_context: PlannerConversationContext | None,
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
    candidate_acceptance: PipelineCandidateAcceptance | None,
    terminal_contract: PlannerTerminalContract,
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
    discovery_policy = PlannerDiscoveryPolicy.initial(
        surface,
        required_catalog_detail_tools=discovery_digest_detail_tools(authoring_aids),
    )
    information_manifest = discovery_policy.manifest
    declared_pending_information = frozenset(information_manifest.unresolved) | frozenset(_intent_selected_schema_keys(intent))
    pending_information = set(declared_pending_information)
    tools = planner_tool_definitions(discovery_policy, terminal_contract=terminal_contract)
    provider_request: dict[str, Any] = {
        "intent": intent,
        "current_state": provider_current_state,
        "reviewed_facts": reviewed_planner_context,
        "authoring_aids": authoring_aids,
        "information_manifest": information_manifest.provider_payload(
            discoverable_classes=discovery_policy.unresolved_classes,
            unresolved_keys=frozenset(pending_information),
        ),
        "instruction": terminal_contract.instruction or PLANNER_TERMINAL_INSTRUCTION,
    }
    if conversation_context is not None:
        provider_request["conversation_context"] = conversation_context.to_dict()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": rendered_skill},
        {
            "role": "user",
            "content": canonical_json(provider_request),
        },
    ]
    total_calls = 0
    total_cost = Decimal("0")
    discovery_turns = 0
    composition_turns = 0
    repair_count = 0
    prose_nudges = 0
    decline_notice_given = False
    nodeless_nudge_given = False
    threshold_nudge_given = False
    # Computed once: the current instruction and bounded earlier-user context
    # are fixed for the whole planning request.
    stated_threshold = _stated_threshold_for_planner_request(intent, conversation_context)
    seen_discovery: set[tuple[str, str]] = set()
    seen_discovery_round = 0
    no_gain_calls_in_round = 0
    # Account for selected plugin contracts as the exact canonical aggregate
    # supplied to the planner, including the enclosing list and separators.
    # Summing each contract independently creates a small but real gap at the
    # 48 KiB boundary.
    selected_schema_contracts: list[dict[str, Any]] = []
    # (component, code) fingerprints of every candidate rejection so far in
    # this request. A repeat means the intervening repair changed nothing that
    # mattered — the feedback then says so explicitly (repeat_notice) instead
    # of letting the model burn budget without knowing it is looping.
    seen_rejection_fingerprints: set[tuple[tuple[str, str], ...]] = set()

    def _violated_plugin_contract(kind: PluginKind, plugin_name: str) -> Mapping[str, Any] | None:
        """Rehydrate and charge one violated plugin's planner contract.

        Defined beside the ledger it charges rather than at the rejection
        site, so the aggregate contract budget has exactly one mutator.

        ``get_schema`` raising here is a Tier-1 anomaly, not a miss: the
        candidate reached option prevalidation only because
        ``_validate_plugin_name`` already resolved this identity through this
        same request-scoped policy view, so a failure means the view changed
        under the request. That propagates, exactly as
        ``build_plugin_schemas_for_failure`` documents for the freeform twin.
        A schema the BOUNDED projection cannot represent is an ordinary miss
        and simply goes unattached — the model still has
        ``get_plugin_assistance``.
        """
        contract, projection_available = _project_planner_plugin_contract(
            policy_catalog.get_schema(kind, plugin_name),
        )
        if not projection_available:
            return None
        assert contract is not None
        contract_payload = contract.to_dict()
        candidate_contracts = [*selected_schema_contracts, contract_payload]
        if len(canonical_json(candidate_contracts).encode("utf-8")) > _SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES:
            return None
        selected_schema_contracts.append(contract_payload)
        return contract_payload

    async def call_model(
        *,
        model_override: str | None = None,
        tools_override: list[dict[str, Any]] | None = None,
        allow_text_reply: bool = False,
        text_reply_marker: str | None = None,
        reasoning_effort: str | None = None,
        attempt_phase_hint: ComposerPlannerAttemptPhase = ComposerPlannerAttemptPhase.RESPONSE,
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
            canonical_schema=terminal_contract.schema,
            capability_schema=canonical_set_pipeline_schema(),
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

        def begin_response_attempt(
            call_to_bind: ComposerLLMCall,
            parsed_calls: tuple[_ParsedToolCall, ...] = (),
        ) -> None:
            planner_call_ordinal = call_to_bind.planner_call_ordinal
            if planner_call_ordinal is None:
                raise AuditIntegrityError("planner semantic attempt requires a physical call ordinal")
            selected_tools = tuple(_planner_tool_name(parsed_call.name) for parsed_call in parsed_calls)
            requested_information = tuple(
                information_class
                for parsed_call in parsed_calls
                for information_class in _planner_information_classes(planner_discovery_information_keys(parsed_call))
            )
            candidate_shape_hash: str | None = None
            for parsed_call in parsed_calls:
                if parsed_call.name != _TERMINAL_TOOL_NAME:
                    continue
                arguments = deep_thaw(parsed_call.arguments)
                if type(arguments) is not dict:
                    continue
                candidate = arguments["pipeline"] if "pipeline" in arguments else None
                if type(candidate) is dict:
                    candidate_shape_hash = _candidate_shape_hash(candidate)
                break
            trail.begin_attempt(
                planner_call_ordinal=planner_call_ordinal,
                phase_hint=attempt_phase_hint,
                selected_tools=selected_tools,
                requested_information=requested_information,
                candidate_shape_hash=candidate_shape_hash,
            )

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
            apply_reasoning_kwargs(kwargs, model=effective_model, effort=reasoning_effort)
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
            try:
                _assert_planner_call_matches_manifest(call, manifest, recorder)
            except AuditIntegrityError:
                begin_response_attempt(call)
                raise
            # Cost enforcement is intentionally post-call and pre-parse.  Do
            # not inspect provider content or dispatch tools before it passes.
            if call.provider_cost is None:
                recorder.record_llm_call(call)
                begin_response_attempt(call)
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
                begin_response_attempt(call)
                raise malformed_usage
            if call.completion_tokens > budget_policy.max_completion_tokens:
                recorder.record_llm_call(call)
                begin_response_attempt(call)
                raise PipelinePlannerError(
                    "planner provider reported a completion token limit overage",
                    code="COMPLETION_TOKENS_EXCEEDED",
                )
            total_cost += Decimal(str(call.provider_cost))
            if total_cost > budget_policy.max_cumulative_provider_cost:
                recorder.record_llm_call(call)
                begin_response_attempt(call)
                raise PipelinePlannerError("planner provider cost continuation cap exceeded", code="COST_CAP_EXCEEDED")
            try:
                parsed_response = _parse_response_tool_calls(
                    response,
                    max_tool_calls=model_config.max_tool_calls_per_turn,
                    allow_text=allow_text_reply,
                    text_marker=text_reply_marker,
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
                begin_response_attempt(call)
                if truncated:
                    raise PipelinePlannerError(
                        "planner response was truncated at the completion token limit",
                        code="RESPONSE_TRUNCATED",
                    ) from exc
                raise
            recorder.record_llm_call(call)
            message, calls = parsed_response
            begin_response_attempt(call, calls)
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

    def _rejection_exhausted(message: str = "planner repair budget exhausted") -> PipelinePlannerError:
        # The blind-repeat short-circuit passes its own honest message; the
        # CODE stays REPAIR_EXHAUSTED either way — one downstream envelope
        # (``planner_repair_exhausted``) covers every repair non-convergence.
        return PipelinePlannerError(
            message,
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

    def _engage_escape_hatch(error: PipelinePlannerError) -> None:
        nonlocal hatch_error, hatch_turn_next, hatch_spent
        hatch_error = error
        hatch_turn_next = True
        hatch_spent = True
        messages.append({"role": "user", "content": _escape_hatch_notice()})

    def _retain_terminal_rejection(
        *,
        provider_message: Any,
        parsed_calls: tuple[_ParsedToolCall, ...],
        terminal_call: _ParsedToolCall,
        feedback: Mapping[str, Any],
    ) -> None:
        # A rejected terminal proposal was evaluated and its safe result is
        # known, so retain the exact provider-authored tool call together with
        # that allowlisted result. This closes the tool protocol without
        # projecting any candidate-finalizer authority back to the advisor.
        messages.append(_assistant_tool_calls_message(provider_message, parsed_calls))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": terminal_call.call_id,
                "content": canonical_json(feedback),
            }
        )

    while True:
        is_hatch_turn = hatch_turn_next
        hatch_turn_next = False
        # A text decline is legal on an ordinary turn once catalog.selection
        # is supplied and no declared or requested information key remains
        # unresolved — vacuously true on turn 1 for a gapless request, which
        # permits a one-call decline by design. Because eligible turns keep
        # the full tool palette, classification requires the taught marker
        # prefix; marker-less text keeps the nudge treatment. The hatch turn
        # accepts any text (its advisor is tool-restricted and taught by its
        # own notice).
        prose_decline_eligible = information_manifest.supplies(_CATALOG_SELECTION_INFORMATION) and all(
            information_manifest.covers(key) for key in pending_information
        )
        if prose_decline_eligible and not is_hatch_turn and not decline_notice_given:
            decline_notice_given = True
            messages.append({"role": "user", "content": _prose_decline_notice()})
        try:
            if is_hatch_turn:
                assert model_config.escape_hatch_model is not None
                assert hatch_error is not None
                message, calls, audited_call = await call_model(
                    model_override=model_config.escape_hatch_model,
                    tools_override=[planner_terminal_tool_definition(terminal_contract)],
                    allow_text_reply=True,
                    reasoning_effort=model_config.candidate_reasoning_effort,
                    attempt_phase_hint=ComposerPlannerAttemptPhase.HATCH,
                )
            else:
                message, calls, audited_call = await call_model(
                    allow_text_reply=prose_decline_eligible,
                    text_reply_marker=_PROSE_DECLINE_MARKER,
                    reasoning_effort=(
                        # A prose reply is the model announcing it is at
                        # emission stage: at discovery effort "low" sonnet-5
                        # emitted zero reasoning on exactly the turns where
                        # the terminal proposal was due and narrated the plan
                        # as prose instead, and this branch could never give
                        # the first emission turn candidate-level effort —
                        # candidate effort only engaged after a REJECTED
                        # candidate (elspeth-b1e85829e9, 2/2 live repro;
                        # effort=medium produced a real candidate on the same
                        # repro). Sticky past the first nudge by design: once
                        # the model has prose-planned, emission is imminent.
                        model_config.candidate_reasoning_effort
                        if repair_count > 0 or prose_nudges > 0
                        else model_config.discovery_reasoning_effort
                    ),
                )
        except PipelinePlannerError as exc:
            if exc.code == "PROSE_REPLY":
                # A no-tool-call reply (thinking aloud) mid-plan: nudge the
                # model back to tool calling on its own bounded budget —
                # separate from the repair budget, which answers candidate
                # rejections. Past the budget the puzzle goes to the escape
                # hatch like every other exhaustion; the terminal
                # MALFORMED_RESPONSE disposition stands only when no hatch
                # is available.
                if is_hatch_turn:
                    # The advisor's one shot produced nothing usable: the
                    # hatch is spent, the original exhaustion stands.
                    trail.finish_attempt("hatch", "prose_reply", led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                prose_nudges += 1
                if prose_nudges > _PROSE_NUDGE_BUDGET:
                    if _hatch_available():
                        trail.finish_attempt("prose", "prose_reply", planner_code="MALFORMED_RESPONSE", led_to="hatch")
                        _engage_escape_hatch(PipelinePlannerError("planner response must call a declared tool", code="MALFORMED_RESPONSE"))
                        continue
                    trail.finish_attempt("prose", "prose_reply", planner_code="MALFORMED_RESPONSE", led_to="terminal")
                    raise PipelinePlannerError(
                        "planner response must call a declared tool",
                        code="MALFORMED_RESPONSE",
                    ) from exc
                trail.finish_attempt("prose", "prose_nudged", led_to="continue")
                messages.append({"role": "user", "content": _prose_reply_notice()})
                continue
            if exc.code != "RESPONSE_TRUNCATED":
                if is_hatch_turn:
                    trail.finalize_active_exception(exc)
                    assert hatch_error is not None
                    raise hatch_error from None
                raise
            if is_hatch_turn:
                # The advisor's one shot overflowed: the hatch is spent, the
                # original exhaustion stands.
                trail.finish_attempt("hatch", "truncated", led_to="terminal")
                assert hatch_error is not None
                raise hatch_error from None
            repair_count += 1
            if repair_count > repair_budget:
                if _hatch_available():
                    trail.finish_attempt("repair", "truncated", planner_code="REPAIR_EXHAUSTED", led_to="hatch")
                    _engage_escape_hatch(PipelinePlannerError("planner repair budget exhausted", code="REPAIR_EXHAUSTED"))
                    continue
                trail.finish_attempt("repair", "truncated", planner_code="REPAIR_EXHAUSTED", led_to="terminal")
                raise PipelinePlannerError("planner repair budget exhausted", code="REPAIR_EXHAUSTED") from None
            trail.finish_attempt("repair", "truncated", led_to="repair")
            messages.append({"role": "user", "content": _truncated_response_notice()})
            continue
        except Exception as exc:
            if is_hatch_turn:
                trail.finalize_active_exception(exc)
                assert hatch_error is not None
                raise hatch_error from None
            raise
        terminal_calls = tuple(call for call in calls if call.name == _TERMINAL_TOOL_NAME)
        # Phase of a terminal-tool turn: the first candidate is "candidate",
        # every post-rejection retry is "repair"; the advisor turn is "hatch";
        # an admitted no-tool-call text reply is "prose".
        attempt_phase = (
            ComposerPlannerAttemptPhase.HATCH
            if is_hatch_turn
            else ComposerPlannerAttemptPhase.CANDIDATE
            if terminal_calls and repair_count == 0
            else ComposerPlannerAttemptPhase.REPAIR
            if terminal_calls
            else ComposerPlannerAttemptPhase.PROSE
            if not calls
            else ComposerPlannerAttemptPhase.DISCOVERY
        )
        trail.set_active_phase(attempt_phase)
        if not calls and (is_hatch_turn or prose_decline_eligible):
            decline_content = _provider_field(message, "content")
            raw_text = decline_content if type(decline_content) is str else ""
            # The parser admitted an ordinary-turn text reply only with the
            # marker present; stripping that protocol token is classification,
            # and the body is the model's words verbatim. The hatch turn keeps
            # its any-text rule, content untouched.
            marker_body = _marked_decline_body(raw_text, _PROSE_DECLINE_MARKER)
            trail.finish_attempt(attempt_phase, "declined", planner_code="DECLINED", led_to="terminal")
            raise PlannerDeclined(
                "planner escape-hatch advisor declined the request" if is_hatch_turn else "planner declined the request",
                decline_text=raw_text if is_hatch_turn else (marker_body if marker_body is not None else ""),
            )
        if len(calls) > model_config.max_tool_calls_per_turn:
            trail.finish_attempt(
                attempt_phase, "budget_exhausted", planner_code="TOOL_CALLS_EXHAUSTED", led_to="terminal", tool_calls=len(calls)
            )
            if is_hatch_turn:
                assert hatch_error is not None
                raise hatch_error from None
            raise PipelinePlannerError("planner per-turn tool call budget exhausted", code="TOOL_CALLS_EXHAUSTED")

        if terminal_calls:
            if not is_hatch_turn:
                composition_turns += 1
                if composition_turns > model_config.max_composition_turns:
                    if _hatch_available():
                        trail.finish_attempt(attempt_phase, "budget_exhausted", planner_code="COMPOSITION_EXHAUSTED", led_to="hatch")
                        _engage_escape_hatch(
                            PipelinePlannerError("planner composition turn budget exhausted", code="COMPOSITION_EXHAUSTED")
                        )
                        continue
                    trail.finish_attempt(attempt_phase, "budget_exhausted", planner_code="COMPOSITION_EXHAUSTED", led_to="terminal")
                    raise PipelinePlannerError("planner composition turn budget exhausted", code="COMPOSITION_EXHAUSTED")
            call = terminal_calls[0]
            terminal_feedback: Mapping[str, Any] | None = None
            # Only fingerprinted feedback kinds can repeat; schema and
            # deferred-claim feedback carry no rejection identity to compare.
            repeated_terminal_fingerprint = False
            pipeline: dict[str, Any] | None = None
            materializer_owned_refs = _FINALIZER_OWNS_NOTHING
            claimed_deferred_intent_ids: tuple[str, ...] = ()
            allowed_terminal_keys = {"pipeline", "claimed_deferred_intent_ids"}
            if "pipeline" not in call.arguments or set(call.arguments) - allowed_terminal_keys:
                terminal_feedback = _canonical_schema_feedback()
            else:
                try:
                    payload = _PlannerTerminalPayload.model_validate(deep_thaw(call.arguments))
                except ValueError as exc:
                    claim_shape_error = isinstance(exc, PydanticValidationError) and any(
                        error["loc"] and error["loc"][0] == "claimed_deferred_intent_ids" for error in exc.errors()
                    )
                    terminal_feedback = _deferred_intent_claim_feedback() if claim_shape_error else _canonical_schema_feedback()
                else:
                    claimed_deferred_intent_ids = tuple(str(intent_id) for intent_id in payload.claimed_deferred_intent_ids)
                    schema_errors = (
                        []
                        if payload.pipeline is None
                        else list(Draft202012Validator(terminal_contract.schema).iter_errors(payload.pipeline))
                    )
                    if not set(claimed_deferred_intent_ids).issubset(eligible_deferred_intent_ids):
                        terminal_feedback = _deferred_intent_claim_feedback()
                    elif schema_errors:
                        # The structural pre-check runs ahead of Stage 1, so a
                        # naming or shape violation never reaches the validator
                        # whose codes name the failing field. Carrying the JSON
                        # path and the violated rule keeps this gate as
                        # actionable as its Stage-1 counterparts
                        # (node_id_invalid / connection_label_invalid) instead
                        # of spending a repair turn on "somewhere, something".
                        schema_violations, schema_violations_withheld = _structural_schema_violations(schema_errors)
                        terminal_feedback = _canonical_schema_feedback(
                            schema_violations,
                            violations_withheld=schema_violations_withheld,
                        )
                        _log_schema_precheck_rejection(trail, schema_errors)
                    else:
                        (
                            pipeline,
                            materializer_owned_refs,
                            terminal_feedback,
                            repeated_terminal_fingerprint,
                        ) = _materialize_terminal_payload(
                            payload=payload.pipeline,
                            terminal_contract=terminal_contract,
                            seen_rejection_fingerprints=seen_rejection_fingerprints,
                        )
            finalized_pipeline: Mapping[str, Any] | None = None
            finalizer_owned_refs: _FinalizerOwnedRefs = _FINALIZER_OWNS_NOTHING
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
                            if is_hatch_turn:
                                trail.finalize_active_exception(exc)
                                assert hatch_error is not None
                                raise hatch_error from None
                            raise
                        # The reviewed-authority binder rejected the shape the
                        # planner authored — repairable in one budgeted turn,
                        # never a 500. Typed rejections carry their closed
                        # code and custody-safe connectivity facts
                        # (elspeth-572c642dbf). Every binder site in ``src/``
                        # now raises the typed form, so the ``else`` arm is
                        # production-unreachable and stays only as the
                        # fail-closed net for a future untyped site: an
                        # unclassified candidate-shape complaint must still
                        # reach the planner as a repairable rejection rather
                        # than a 500.
                        if type(exc) is GuidedCandidateBindingRejected:
                            binding_fingerprint = _binding_rejection_fingerprint(exc)
                            repeated_binding_fingerprint = binding_fingerprint in seen_rejection_fingerprints
                            seen_rejection_fingerprints.add(binding_fingerprint)
                            terminal_feedback = _binding_rejection_feedback(exc, repeated_fingerprint=repeated_binding_fingerprint)
                        else:
                            terminal_feedback = _canonical_schema_feedback()
                    except Exception as exc:
                        if is_hatch_turn:
                            trail.finalize_active_exception(exc)
                            assert hatch_error is not None
                            raise hatch_error from None
                        raise
                    else:
                        if type(finalizer_result) is not dict:
                            finalizer_error = AuditIntegrityError("pipeline candidate finalizer must return an exact dict")
                            if is_hatch_turn:
                                trail.finalize_active_exception(finalizer_error)
                                assert hatch_error is not None
                                raise hatch_error from None
                            raise finalizer_error
                        finalized_pipeline = finalizer_result
                        # Entry-scoped custody attribution (elspeth-5904b1683a):
                        # derived by diff HERE, not self-reported by the
                        # finalizer, so a pass that mutates without declaring
                        # can never leak server-bound validator detail.
                        finalizer_refs = _derive_finalizer_owned_refs(pipeline, finalizer_result)
                        finalizer_owned_refs = _FinalizerOwnedRefs(
                            config=materializer_owned_refs.config | finalizer_refs.config,
                            routing=materializer_owned_refs.routing | finalizer_refs.routing,
                        )
            if terminal_feedback is not None:
                last_rejection_codes = _feedback_error_codes(terminal_feedback)
                if is_hatch_turn:
                    trail.finish_attempt(
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
                        trail.finish_attempt(
                            attempt_phase,
                            "candidate_rejected",
                            codes=last_rejection_codes,
                            planner_code="REPAIR_EXHAUSTED",
                            led_to="hatch",
                            repeated_fingerprint=repeated_terminal_fingerprint,
                        )
                        _retain_terminal_rejection(
                            provider_message=message,
                            parsed_calls=calls,
                            terminal_call=call,
                            feedback=terminal_feedback,
                        )
                        _engage_escape_hatch(_rejection_exhausted())
                        continue
                    trail.finish_attempt(
                        attempt_phase,
                        "candidate_rejected",
                        codes=last_rejection_codes,
                        planner_code="REPAIR_EXHAUSTED",
                        led_to="terminal",
                        repeated_fingerprint=repeated_terminal_fingerprint,
                    )
                    raise _rejection_exhausted() from None
                trail.finish_attempt(
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
                if unproducible_output_fields and _transform_node_count(finalized_pipeline) == 0:
                    # R2-F4 (elspeth-6e311df389). The reviewed outputs declare
                    # fields no reviewed source declares or observes, and this
                    # candidate has nothing that could produce them. Every
                    # guided plan arrives here as an ordinary planner request
                    # (the server-synthesized sketch this guard once diverted
                    # was removed with elspeth-b4a286d517); without it a
                    # planner that answers with a bare pass-through seals an
                    # unbuildable pipeline as a COMPLETE proposal.
                    #
                    # Unlike the two nudges around it this fires on EVERY
                    # attempt including the hatch, and has no omit-valve: the
                    # claim is a mechanical set difference over reviewed facts,
                    # not an inference from prose, and adding any transform
                    # clears it in one turn. An identical re-emit draws the
                    # ordinary repeat notice through the shared fingerprint
                    # path rather than being waved through.
                    #
                    # ORDERING IS LOAD-BEARING (T1xT3, acceptance-r2 final
                    # review): this guard must precede the nodeless-revision
                    # nudge below. Both trigger on the same zero-transform
                    # shape, but the nudge's omit-valve promises that an
                    # unchanged re-emit "will be accepted" while this guard
                    # rejects exactly that re-emit — two contradictory repair
                    # instructions against a repair budget of 2 is a
                    # guaranteed unrepairable path. Firing this guard first
                    # gives the model ONE coherent instruction that names the
                    # missing fields, and adding a transform clears both
                    # guards' trigger in the same turn; the nudge's promise is
                    # then only ever made when it is true.
                    raise _PipelineCandidateRejected(
                        _unproducible_output_fields_rejection(current_state, fields=unproducible_output_fields)
                    )
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
                    # there guarantees failure. Only reachable when
                    # ``unproducible_output_fields`` is empty — the
                    # satisfiability guard above owns the zero-transform shape
                    # otherwise (see its ordering note).
                    nodeless_nudge_given = True
                    raise _PipelineCandidateRejected(_nodeless_revision_rejection(current_state))
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
                    candidate_acceptance=candidate_acceptance,
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
                    trail.finish_attempt("hatch", "deferred_claim", codes=last_rejection_codes, led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                repair_count += 1
                if repair_count > repair_budget:
                    if _hatch_available():
                        trail.finish_attempt(
                            attempt_phase, "deferred_claim", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="hatch"
                        )
                        deferred_feedback = _deferred_intent_claim_feedback()
                        _retain_terminal_rejection(
                            provider_message=message,
                            parsed_calls=calls,
                            terminal_call=call,
                            feedback=deferred_feedback,
                        )
                        _engage_escape_hatch(_rejection_exhausted())
                        continue
                    trail.finish_attempt(
                        attempt_phase, "deferred_claim", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="terminal"
                    )
                    raise _rejection_exhausted() from None
                trail.finish_attempt(attempt_phase, "deferred_claim", codes=last_rejection_codes, led_to="repair")
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
                    trail.finish_attempt("hatch", "arg_error", codes=last_rejection_codes, led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                repair_count += 1
                if repair_count > repair_budget:
                    if _hatch_available():
                        trail.finish_attempt(
                            attempt_phase, "arg_error", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="hatch"
                        )
                        argument_feedback = _allowlisted_argument_feedback(exc)
                        _retain_terminal_rejection(
                            provider_message=message,
                            parsed_calls=calls,
                            terminal_call=call,
                            feedback=argument_feedback,
                        )
                        _engage_escape_hatch(_rejection_exhausted())
                        continue
                    trail.finish_attempt(
                        attempt_phase, "arg_error", codes=last_rejection_codes, planner_code="REPAIR_EXHAUSTED", led_to="terminal"
                    )
                    raise _rejection_exhausted() from None
                trail.finish_attempt(attempt_phase, "arg_error", codes=last_rejection_codes, led_to="repair")
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
                assert finalized_pipeline is not None
                assert pipeline is not None
                last_rejection_codes = _candidate_rejection_codes(exc.result)
                rejection_fingerprint = _rejection_fingerprint(exc.result)
                repeated_fingerprint = rejection_fingerprint in seen_rejection_fingerprints
                seen_rejection_fingerprints.add(rejection_fingerprint)
                candidate_facts_withheld = _rejection_facts_withheld(exc.result, finalizer_owned_refs)

                candidate_feedback = _allowlisted_candidate_feedback(
                    exc.result,
                    repeated_fingerprint=repeated_fingerprint,
                    finalizer_owned=finalizer_owned_refs,
                    components_withheld=_withheld_component_count(exc.result),
                    plugin_contract_resolver=_violated_plugin_contract,
                )
                if os.environ.get("ELSPETH_PLANNER_REJECTION_DETAIL_LOG") == "1":
                    # Operator-opted diagnostic seam. Validator messages are never
                    # logged: even an opt-in diagnostic must not persist authored
                    # option values, row content, paths, or secret material. Closed
                    # codes plus component KIND/severity retain the useful
                    # classifier. The component's identifier half (a node id or
                    # sink name) is model-authored text too — the planner picks
                    # those names — so only the kind prefix is logged; the full
                    # ``output:<name>`` leaked as soon as any output-attributed
                    # rule fired (elspeth-2ed41f0a4a).
                    slog.warning(
                        "composer.planner_rejection_detail",
                        session_id=trail.session_id,
                        operation_id=trail.operation_id,
                        attempt=trail.attempts,
                        entries=[
                            {
                                "component": entry.component.split(":", 1)[0],
                                "error_code": entry.error_code or "validation_error",
                                "severity": entry.severity,
                            }
                            for entry in _rejection_entries(exc.result)
                        ],
                    )
                if is_hatch_turn:
                    trail.finish_attempt("hatch", "candidate_rejected", codes=last_rejection_codes, led_to="terminal")
                    assert hatch_error is not None
                    raise hatch_error from None
                if repeated_fingerprint and candidate_facts_withheld:
                    # Repeat-while-blind short-circuit (elspeth-5904b1683a): the
                    # identical rejection set has already been answered once,
                    # and at least one entry's candidate facts are withheld —
                    # the model cannot see, and can never fix, the server-bound
                    # configuration behind it. Burning the remaining budget on
                    # near-identical candidates is deterministic waste, so this
                    # resolves straight to the terminal path (hatch first, as
                    # the budget-exhaustion path does). Budget semantics are
                    # unchanged whenever every entry carries its facts.
                    if _hatch_available():
                        trail.finish_attempt(
                            attempt_phase,
                            "candidate_rejected",
                            codes=last_rejection_codes,
                            planner_code="REPAIR_BLIND_REPEAT",
                            led_to="hatch",
                            repeated_fingerprint=True,
                        )
                        _retain_terminal_rejection(
                            provider_message=message,
                            parsed_calls=calls,
                            terminal_call=call,
                            feedback=candidate_feedback,
                        )
                        _engage_escape_hatch(
                            _rejection_exhausted(
                                "planner repair short-circuited: repeated rejection with withheld candidate facts cannot converge"
                            )
                        )
                        continue
                    trail.finish_attempt(
                        attempt_phase,
                        "candidate_rejected",
                        codes=last_rejection_codes,
                        planner_code="REPAIR_BLIND_REPEAT",
                        led_to="terminal",
                        repeated_fingerprint=True,
                    )
                    raise _rejection_exhausted(
                        "planner repair short-circuited: repeated rejection with withheld candidate facts cannot converge"
                    ) from None
                repair_count += 1
                if repair_count > repair_budget:
                    if _hatch_available():
                        trail.finish_attempt(
                            attempt_phase,
                            "candidate_rejected",
                            codes=last_rejection_codes,
                            planner_code="REPAIR_EXHAUSTED",
                            led_to="hatch",
                            repeated_fingerprint=repeated_fingerprint,
                        )
                        _retain_terminal_rejection(
                            provider_message=message,
                            parsed_calls=calls,
                            terminal_call=call,
                            feedback=candidate_feedback,
                        )
                        _engage_escape_hatch(_rejection_exhausted())
                        continue
                    trail.finish_attempt(
                        attempt_phase,
                        "candidate_rejected",
                        codes=last_rejection_codes,
                        planner_code="REPAIR_EXHAUSTED",
                        led_to="terminal",
                        repeated_fingerprint=repeated_fingerprint,
                    )
                    raise _rejection_exhausted() from None
                trail.finish_attempt(
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
            except Exception as exc:
                if is_hatch_turn:
                    trail.finalize_active_exception(exc)
                    assert hatch_error is not None
                    raise hatch_error from None
                raise
            trail.finish_attempt(attempt_phase, "accepted", led_to="done", tool_calls=len(calls))
            return accepted_plan

        if is_hatch_turn:
            # The advisor did anything other than one clean terminal proposal
            # or an honest text decline: the hatch is spent, the original
            # exhaustion stands.
            trail.finish_attempt(
                "hatch",
                "guard_fired",
                planner_code="DISCOVERY_ONLY",
                led_to="terminal",
                tool_calls=len(calls),
            )
            assert hatch_error is not None
            raise hatch_error
        if any(call.name not in _PLANNER_DISCOVERY_TOOL_NAME_SET for call in calls):
            trail.finish_attempt("discovery", "guard_fired", planner_code="DISCOVERY_ONLY", led_to="terminal", tool_calls=len(calls))
            raise PipelinePlannerError(
                "planner may execute read-only discovery tools only before its terminal proposal",
                code="DISCOVERY_ONLY",
            )
        discovery_turns += 1
        if discovery_turns > model_config.max_discovery_turns:
            if _hatch_available():
                trail.finish_attempt(
                    "discovery", "budget_exhausted", planner_code="DISCOVERY_EXHAUSTED", led_to="hatch", tool_calls=len(calls)
                )
                _engage_escape_hatch(PipelinePlannerError("planner discovery turn budget exhausted", code="DISCOVERY_EXHAUSTED"))
                continue
            trail.finish_attempt(
                "discovery", "budget_exhausted", planner_code="DISCOVERY_EXHAUSTED", led_to="terminal", tool_calls=len(calls)
            )
            raise PipelinePlannerError("planner discovery turn budget exhausted", code="DISCOVERY_EXHAUSTED")
        if repair_count != seen_discovery_round:
            # Exact-call repetition stays round-scoped, but semantic
            # information facts remain request-scoped: a successfully read
            # schema is not forgotten just because a candidate was rejected.
            seen_discovery.clear()
            seen_discovery_round = repair_count
            no_gain_calls_in_round = 0
        information_keys = {call.call_id: planner_discovery_information_keys(call) for call in calls}
        no_gain_calls = tuple(
            call
            for call in calls
            if information_keys[call.call_id] and all(information_manifest.covers(key) for key in information_keys[call.call_id])
        )
        useful_calls = tuple(call for call in calls if call not in no_gain_calls)
        no_gain_calls_in_round += len(no_gain_calls)
        escalate_no_gain = bool(no_gain_calls) and no_gain_calls_in_round >= 2
        for call in useful_calls:
            pending_information.update(information_keys[call.call_id])
        keys = tuple((call.name, stable_hash(call.arguments)) for call in useful_calls)
        if any(key in seen_discovery for key in keys) or len(set(keys)) != len(keys):
            # A cycling planner is stuck by definition — hand the puzzle to
            # the advisor rather than failing the request.
            if _hatch_available():
                trail.finish_attempt("discovery", "guard_fired", planner_code="DISCOVERY_CYCLE", led_to="hatch", tool_calls=len(calls))
                _engage_escape_hatch(PipelinePlannerError("planner discovery repetition/cycle guard fired", code="DISCOVERY_CYCLE"))
                continue
            trail.finish_attempt("discovery", "guard_fired", planner_code="DISCOVERY_CYCLE", led_to="terminal", tool_calls=len(calls))
            raise PipelinePlannerError("planner discovery repetition/cycle guard fired", code="DISCOVERY_CYCLE")
        seen_discovery.update(keys)
        await emit_progress(lifecycle.progress, tool_batch_progress_event(tuple(call.name for call in calls)))
        messages.append(_assistant_tool_calls_message(message, calls))
        for call in useful_calls:
            await emit_progress(lifecycle.progress, tool_started_progress_event(call.name))

        if not useful_calls:
            trail.finish_attempt(
                "discovery",
                "guard_fired",
                planner_code="DISCOVERY_NO_GAIN",
                led_to="hatch" if escalate_no_gain and _hatch_available() else "terminal" if escalate_no_gain else "continue",
                tool_calls=len(calls),
            )
            for call in no_gain_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": canonical_json(
                            {
                                "success": False,
                                "error_code": "DISCOVERY_NO_GAIN",
                                "information_keys": list(information_keys[call.call_id]),
                                "message": "The requested information is already supplied or unavailable.",
                            }
                        ),
                    }
                )
            if escalate_no_gain:
                if _hatch_available():
                    _engage_escape_hatch(PipelinePlannerError("planner discovery produced no new information", code="DISCOVERY_NO_GAIN"))
                    continue
                raise PipelinePlannerError("planner discovery produced no new information", code="DISCOVERY_NO_GAIN")
            continue

        async def execute_one_discovery(call: _ParsedToolCall) -> tuple[_ParsedToolCall, ToolResult, bool]:
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
                return (
                    call,
                    ToolResult(
                        success=False,
                        updated_state=current_state,
                        validation=current_state.validate(),
                        affected_nodes=(),
                        data=dict(feedback),
                    ),
                    False,
                )
            result = cast(_AuditedDiscoveryResult, audited.result).result
            return call, result, True

        discovery_tasks = [asyncio.create_task(execute_one_discovery(call)) for call in useful_calls]
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
        discovery_results = iter(task.result() for task in discovery_tasks)
        new_information: list[ComposerPlannerInformationClass] = []
        for call in calls:
            if call in no_gain_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": canonical_json(
                            {
                                "success": False,
                                "error_code": "DISCOVERY_NO_GAIN",
                                "information_keys": list(information_keys[call.call_id]),
                                "message": "The requested information is already supplied or unavailable.",
                            }
                        ),
                    }
                )
                continue
            result_call, result, information_resolved = next(discovery_results)
            if result_call is not call:
                raise AuditIntegrityError("planner discovery result order diverged from admitted calls")
            encoded_contracts = len(canonical_json(selected_schema_contracts).encode("utf-8"))
            budget_remaining = _SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES - encoded_contracts - (1 if selected_schema_contracts else 0)
            serialized_result = _serialize_provider_discovery_result(
                call=call,
                result=result,
                surface=surface,
                provider_current_state=provider_current_state,
                schema_contract_budget_remaining=budget_remaining,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": serialized_result,
                }
            )
            information_available = result.success
            newly_covered_keys = tuple(key for key in information_keys[call.call_id] if not information_manifest.covers(key))
            if call.name == "get_plugin_schema" and result.success:
                contract, projection_available = _project_planner_plugin_contract(result.data)
                if not projection_available:
                    information_available = False
                else:
                    assert contract is not None
                    contract_payload = contract.to_dict()
                    candidate_contracts = [*selected_schema_contracts, contract_payload]
                    if len(canonical_json(candidate_contracts).encode("utf-8")) > _SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES:
                        information_available = False
                    else:
                        selected_schema_contracts.append(contract_payload)
            if information_resolved:
                information_manifest = information_manifest.with_result(
                    information_keys[call.call_id],
                    available=information_available,
                )
                new_information.extend(_planner_information_classes(newly_covered_keys))
            else:
                pending_information.difference_update(set(information_keys[call.call_id]) - declared_pending_information)
            await emit_progress(lifecycle.progress, tool_completed_progress_event(call.name, result.success))
        if next(discovery_results, None) is not None:
            raise AuditIntegrityError("planner discovery produced an unowned result")
        discovery_policy = discovery_policy.with_manifest(information_manifest)
        tools = planner_tool_definitions(discovery_policy, terminal_contract=terminal_contract)
        trail.finish_attempt(
            "discovery",
            "discovery_executed",
            planner_code="DISCOVERY_NO_GAIN" if escalate_no_gain else None,
            led_to="hatch" if escalate_no_gain and _hatch_available() else "terminal" if escalate_no_gain else "continue",
            tool_calls=len(calls),
            new_information=tuple(new_information),
        )
        if escalate_no_gain:
            if _hatch_available():
                _engage_escape_hatch(PipelinePlannerError("planner discovery produced no new information", code="DISCOVERY_NO_GAIN"))
                continue
            raise PipelinePlannerError("planner discovery produced no new information", code="DISCOVERY_NO_GAIN")
        remaining_discovery = model_config.max_discovery_turns - discovery_turns
        if remaining_discovery == 2:
            messages.append({"role": "user", "content": _discovery_pressure_notice(remaining_discovery)})
        if pending_information and all(information_manifest.supplies(key) for key in pending_information):
            messages.append({"role": "user", "content": _ALL_INFORMATION_GAPS_CLOSED_NOTICE})


__all__ = [
    "PLANNER_DISCOVERY_TOOL_NAMES",
    "GuidedPlannerDecline",
    "PipelineCustodyResult",
    "PipelinePlanResult",
    "PipelinePlannerError",
    "PlannerBudgetPolicy",
    "PlannerCustodyConfig",
    "PlannerDeclined",
    "PlannerDiscoveryPolicy",
    "PlannerInformationManifest",
    "PlannerModelConfig",
    "PlannerOriginatingMessage",
    "PlannerRequestLifecycle",
    "PlannerTerminalContract",
    "PlannerTerminalMaterialization",
    "canonical_planner_terminal_contract",
    "plan_pipeline",
    "planner_discovery_information_keys",
    "planner_terminal_tool_definition",
    "planner_tool_definitions",
]
