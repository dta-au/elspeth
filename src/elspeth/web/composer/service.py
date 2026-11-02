"""ComposerServiceImpl — bounded LLM tool-use loop for pipeline composition.

Uses LiteLLM for provider abstraction. Model configured via
WebSettings.composer_model. Tool calls are executed against
CompositionState + CatalogService.

Dual-counter budget: separate limits for discovery and composition turns.
Discovery cache: cacheable discovery tool results cached per-compose-call
in a local dict variable (not an instance field) to avoid concurrent-request
races.

Layer: L3 (application).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import UUID

if TYPE_CHECKING:
    from elspeth.web.composer.composition_completion import _TerminalNoToolAdvisorGateOutcome
    from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner
    from elspeth.web.composer.continuation_classifier_handoff import ClassifyPostPrimary, ClassifySourceProofStop
    from elspeth.web.composer.continuation_classifier_owned_source import ClassifyReplyRefusalReady
    from elspeth.web.composer.continuation_codec import PrivateChunk
    from elspeth.web.composer.continuation_finished_batch_data import ColdPostBatchPersistData
    from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, _CurrentPhaseRead
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.composer.continuation_semantic_advisor_return import _ClassifyEndInvocationCapture
    from elspeth.web.composer.continuation_tool_failure import TerminalServiceCompletion
    from elspeth.web.composer.redaction_telemetry import RedactionTelemetry
    from elspeth.web.composer.turn_effects import BlobDeliveryAttempt, TurnEvidence
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation_classifier_primary import (
        ClassifierPrimaryIssuer,
        ClassifierReplyDeadline,
        VerifiedClassifierPaidOverflowTerminal,
    )
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt
    from elspeth.web.sessions.continuation_tool_no_execution import ToolNoExecutionRefusal
    from elspeth.web.sessions.protocol import (
        SessionServiceProtocol,
    )
    from elspeth.web.sessions.telemetry import _SessionsTelemetry

import structlog
from openai import OpenAIError
from opentelemetry import metrics
from sqlalchemy import Engine

from elspeth.contracts.blobs import BlobServiceProtocol
from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateToolAuthoring
from elspeth.contracts.composer_llm_audit import (
    ComposerLLMCall,
)
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    GuardedProviderMessage,
    PromptGuardContentPart,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
    validate_prompt_guard_messages,
)
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.compartments import ChatIngressInput, CompositionIngressRecord, chat_ingress_input
from elspeth.web.composer import no_tool_policy as _no_tool_policy
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedToolCall,
    _AdvisorReviewState,
    _CallModelOutcome,
    _ClassifyOutcome,
    _DispatchOutcome,
    _EndCompletionDisposition,
    _PersistOutcome,
    _TerminateOutcome,
    _ToolOutcome,
)
from elspeth.web.composer.advisor_checkpoint import (
    AdvisorCheckpointOwner,
    _AdvisorCheckpointComposeDeadlineExpired,
)
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.availability import ComposerAvailability as ComposerAvailability  # re-export; genuine home is availability.py
from elspeth.web.composer.availability import compute_availability
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.composition_completion import (
    CompositionCompletion,
    _append_interpretation_review_handoff_message,
    _end_disposition_after_terminal_barriers,
    _with_advisor_gate_decision,
)
from elspeth.web.composer.continuation_message_builders import (
    reply_instruction_message,
)
from elspeth.web.composer.control_messages import anti_anchor_control_envelope
from elspeth.web.composer.discovery_cache import (
    RuntimePreflightCache as _RuntimePreflightCache,
)
from elspeth.web.composer.discovery_response import admit_discovery_result, preview_source_proof_stop
from elspeth.web.composer.interpretation_surfacing import InterpretationSurfacing
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.llm_response_parsing import (
    attach_llm_calls,
)
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput
from elspeth.web.composer.progress import (
    emit_progress,
    model_call_progress_event,
)
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, build_prompt_guard_mount, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_rendering import JsonPath, json_content_parts
from elspeth.web.composer.prompts import (
    build_run_diagnostics_messages,
    render_system_prompt,
)
from elspeth.web.composer.protocol import (
    COMPOSER_HISTORY_USER_MESSAGE_ID_KEY,
    ComposerAdmissionRefused,
    ComposerContextUnsupported,
    ComposerConvergenceError,
    ComposerHistoryMessage,
    ComposerResult,
    ComposerServiceError,
    ComposerSettings,
)
from elspeth.web.composer.provider_config import (
    LLM_API_MAX_ATTEMPTS,
    LLM_API_RETRY_BASE_DELAY_SECONDS,
    infer_provider_from_model_name,
    infer_provider_from_unprefixed_model_name,
)
from elspeth.web.composer.provider_errors import classify_provider_failure
from elspeth.web.composer.provider_gateway import (
    ProviderGateway,
    _BadRequestLLMError,
    advisor_provider_failure_types,
    composer_loop_tool_definitions,
)
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily, ProviderInvocationOwner, composer_quota_scope
from elspeth.web.composer.reasoning import warn_if_not_reasoning_capable
from elspeth.web.composer.recovery_controls import (
    parse_recovery_tool_batch,
    recovery_tool_definitions,
)
from elspeth.web.composer.request_history import authoritative_conversation_context
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.composer.session_tool import SessionToolOwner
from elspeth.web.composer.skills import assert_skill_hash_unchanged_on_disk
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.strict_transport import (
    ComposerToolContractSummary,
    StrictTransportDiagnostic,
    resolve_composer_tool_contract,
)
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, ProofUnavailable
from elspeth.web.execution.completion_gates import (
    CompletionGateFacts,
)
from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
from elspeth.web.execution.schemas import (
    ValidationResult,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.secrets.wiring_policy import runtime_secret_wiring_policy
from elspeth.web.sessions._persist_payload import AuditOutcome, RedactedToolRow

slog = structlog.get_logger()


_ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE = _no_tool_policy.ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE
_is_pending_interpretation_handoff = _no_tool_policy.is_pending_interpretation_handoff
_state_is_structurally_empty = _no_tool_policy.state_is_structurally_empty


def _diagnostic_value(diagnostic: StrictTransportDiagnostic | None) -> str | None:
    """A route diagnostic's closed string value for the resolution log, or ``None``."""
    return None if diagnostic is None else diagnostic.value


_INTERPRETATION_REVIEW_HANDOFF_KINDS: Final[frozenset[str]] = frozenset(
    {
        "interpretation_review_pending",
        "interpretation_review_pending_idempotent",
    }
)


def _tool_outcome_is_interpretation_review_handoff(outcome: _ToolOutcome) -> bool:
    response = outcome.response
    if not isinstance(response, ToolResult):
        return False
    data = response.data
    if not isinstance(data, Mapping):
        return False
    return "_kind" in data and data["_kind"] in _INTERPRETATION_REVIEW_HANDOFF_KINDS


def _tool_batch_staged_terminal_interpretation_review_handoff(tool_outcomes: tuple[_ToolOutcome, ...]) -> bool:
    """Return True when clean pending-review calls are the batch's terminal suffix."""

    handoff_seen = False
    for outcome in tool_outcomes:
        if outcome.error_class is not None:
            return False
        response = outcome.response
        if isinstance(response, ToolResult) and not response.success:
            return False
        if _tool_outcome_is_interpretation_review_handoff(outcome):
            handoff_seen = True
            continue
        if handoff_seen:
            return False
    return handoff_seen


_REPLY_ONLY_HISTORY_PREFIX = "Historical tool protocol record (quoted data):\n"
_REPLY_ONLY_HISTORY_SUFFIX = "\n[End historical tool evidence; this was not a current assistant reply.]"


def _reply_only_messages(
    llm_messages: Sequence[GuardedProviderMessage], *, turn_evidence: TurnEvidence | None = None
) -> list[GuardedProviderMessage]:
    """Retain attributed tool evidence as quoted data when no tools are advertised."""
    # Native thinking belongs to the original complete assistant/tool exchange.
    # The caller's explicit recovery-only palette still controls new actions.
    validate_prompt_guard_messages(llm_messages)
    if any(PROMPT_GUARD_THINKING_PARTS_KEY in message and message[PROMPT_GUARD_THINKING_PARTS_KEY] is not None for message in llm_messages):
        return list(llm_messages)
    messages: list[GuardedProviderMessage] = []
    for historical_message in llm_messages:
        if historical_message["role"] != "tool" and "tool_calls" not in historical_message:
            messages.append(historical_message)
            continue
        evidence = detach_prompt_guard_evidence([historical_message])
        wire = evidence.messages[0]
        string_parts: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
        content_parts = evidence.content_parts[0]
        if content_parts is not None:
            string_parts[("content",)] = content_parts
        elif evidence.trusted_message_indexes and "content" in wire and type(wire["content"]) is str:
            string_parts[("content",)] = (
                PromptGuardContentPart(wire["content"], cast(PromptGuardTrust, historical_message[PROMPT_GUARD_TRUST_KEY])),
            )
        arguments = evidence.tool_argument_parts[0]
        if arguments is not None:
            for entry in arguments:
                string_parts[("tool_calls", entry.tool_call_index, "function", "arguments")] = entry.parts
        serialized = json.dumps(wire, ensure_ascii=False, sort_keys=True)
        suffix = _REPLY_ONLY_HISTORY_SUFFIX
        quoted_parts = (
            PromptGuardContentPart(_REPLY_ONLY_HISTORY_PREFIX),
            *json_content_parts(serialized, ensure_ascii=False, sort_keys=True, string_parts=string_parts),
            PromptGuardContentPart(suffix),
        )
        quoted_role = "user" if historical_message["role"] == "tool" else historical_message["role"]
        quoted_content = _REPLY_ONLY_HISTORY_PREFIX + serialized + suffix
        if (
            turn_evidence is not None
            and wire["role"] == "tool"
            and type(wire.get("tool_call_id")) is str
            and type(wire.get("content")) is str
        ):
            turn_evidence.register_quoted_inspection(
                tool_call_id=wire["tool_call_id"],
                original_content=wire["content"],
                quoted_role="user",
                quoted_content=quoted_content,
            )
        messages.append({"role": quoted_role, "content": quoted_content, PROMPT_GUARD_CONTENT_PARTS_KEY: quoted_parts})
    return messages


def _reply_only_protocol_records(content: str) -> tuple[object, ...]:
    """Parse only complete tool-shaped records under our history envelope."""
    decoder = json.JSONDecoder()
    offset = 0
    records: list[object] = []
    while (start := content.find(_REPLY_ONLY_HISTORY_PREFIX, offset)) >= 0:
        offset = start + len(_REPLY_ONLY_HISTORY_PREFIX)
        try:
            record, _ = decoder.raw_decode(content[offset:].lstrip())
        except json.JSONDecodeError:
            continue
        if type(record) is not dict:
            continue
        if record.get("role") == "tool" and type(record.get("tool_call_id")) is str and "content" in record:
            records.append(record)
            continue
        calls = record.get("tool_calls")
        if (
            record.get("role") == "assistant"
            and type(calls) is list
            and calls
            and any(
                type(call) is dict
                and call.get("type") == "function"
                and type(call.get("id")) is str
                and type(call.get("function")) is dict
                and type(call["function"].get("name")) is str
                and type(call["function"].get("arguments")) is str
                for call in calls
            )
        ):
            records.append(record)
    return tuple(records)


def _reply_only_protocol_echo(content: str, *, history_has_tool_protocol: bool, user_message: str) -> bool:
    """Withhold an internal protocol echo while retaining user-quoted log data."""
    if not history_has_tool_protocol:
        return False
    user_quoted_records = _reply_only_protocol_records(user_message)
    return any(record not in user_quoted_records for record in _reply_only_protocol_records(content))


def _announce_staged_review_handoff(result: ComposerResult, raw_content: str | None) -> ComposerResult:
    """Announce a TOOL-BATCH-staged review as EXACTLY ONE canonical suffix.

    The staged-handoff branch of ``_classify_and_budget_turn`` owns the
    announcement whenever the shared finalize tail did not (the tail keys on
    the pending-handoff PREFLIGHT SHAPE; this branch keys on the tool batch, so
    it still owns the None / green / red-for-another-reason cases). The two
    predicates are exact complements, so a given pending handoff is announced
    once — but "announced once" was not the same as "one backend suffix"
    (elspeth-2ed41f0a4a R1).

    The gap: ``finalize_no_tool_response`` may ALREADY have appended a suffix
    of its own before control returns here. Reachability, enumerated against
    ``_reuse_or_recompute_runtime_preflight``:

    * The branch's own recomputation and the tail's agree by construction
      except on ONE arm — the cross-turn arm (elspeth-ac85b0ab0e). The branch
      sees ``None`` (no mutation this call, no ``preview_pipeline``) and
      enters; the tail then pays the preflight anyway because the state is
      WIRED and Stage-1 invalid, and can come back red-but-not-handoff. That
      red takes the ``preflight_invalid_non_empty_state_augmentation`` branch,
      whose suffix this function would have stacked its own on top of.
    * Both EMPTY-state finalize branches are unreachable from here. The
      cross-turn arm requires sources AND outputs, so the state cannot be
      structurally empty; and a review call against a state with no rows fails
      ARG_ERROR, which
      ``_tool_batch_staged_terminal_interpretation_review_handoff`` already
      rejects. The guard below mirrors the tail's own dispatch condition
      rather than asserting that, so the two cannot drift apart.

    Stacking was not merely untidy. ``_canonical_trusted_suffix_segments``
    recognizes a CLOSED set of whole-suffix shapes, so two concatenated
    canonical suffixes match none of them: ``visible_message_segments`` fails
    closed and BOTH disclosures — the validator's objection and the staged
    review — render as model prose. And it passed
    ``_enforce_augmentation_prefix_invariant`` silently, because a doubled
    suffix still leaves the prose a strict prefix.

    The fix keeps both facts and spends one suffix on them: the red result is
    handed back as ``outstanding_findings``, so the objection rides the
    qualified handoff shape's untrusted ``Cause:`` region — the same leading
    objection ``compose_preflight_failure_message`` would have named, since
    both read ``first_validation_objection`` — and its ``Suggested fix:`` tail
    rides along too. The tail's suffix is replaced rather than extended, so
    dropping that suggestion would remove it from the handoff notice.
    ``_composer_persisted_validation`` omits runtime error suggestions; the
    decision panel's reload backfill recomputes separate Stage-1 suggestions
    and does not restore this runtime suggestion.

    """
    runtime_result = result.runtime_preflight
    if (
        runtime_result is not None
        and not runtime_result.is_valid
        and not _is_pending_interpretation_handoff(runtime_result)
        and not _state_is_structurally_empty(result.state)
    ):
        # Rebuild from the model's own prose so the tail's suffix is REPLACED,
        # never extended. ``raw_assistant_content`` is populated on every
        # augmenting tail branch, so the ``or ""`` is a type narrowing rather
        # than a fallback.
        return _append_interpretation_review_handoff_message(
            replace(result, message=result.raw_assistant_content or ""),
            raw_content,
            outstanding_findings=runtime_result,
        )
    return _append_interpretation_review_handoff_message(result, raw_content)


_ADVISOR_ATTEMPTED_ACTIONS_MAX_ITEMS: Final[int] = 8


def _record_advisor_repair_mutations(
    review_state: _AdvisorReviewState,
    tool_outcomes: tuple[_ToolOutcome, ...],
) -> _AdvisorReviewState:
    """Record only successful composition-state mutations after an END FLAG."""
    if review_state.completed_passes == 0:
        return review_state
    actions = list(review_state.successful_mutating_actions)
    for outcome in tool_outcomes:
        if outcome.error_class is not None or outcome.post_version <= outcome.pre_version:
            continue
        tool_name = outcome.call.function.name
        if type(tool_name) is str and tool_name not in actions:
            actions.append(tool_name)
    return replace(
        review_state,
        successful_mutating_actions=tuple(actions[:_ADVISOR_ATTEMPTED_ACTIONS_MAX_ITEMS]),
    )


# The per-dispatch audit envelope (DispatchAudit, begin_dispatch, finish_*)
# and the structural enforcement helper (dispatch_with_audit) live in
# web/composer/audit.py next to the BufferingRecorder. See the audit.py module
# docstring for the contract details.


_TRAINED_OPERATOR_COMPOSITION_ROOT = object()


def _chat_ingress_inputs_for_compose(
    message: str,
    messages: list[ComposerHistoryMessage],
    *,
    user_message_id: str | None,
    own_compartment_id: str | None,
) -> list[ChatIngressInput]:
    """Carry every authenticated request through state writes without paraphrasing."""
    inputs: list[ChatIngressInput] = []
    history = authoritative_conversation_context(message, messages)
    if history is not None:
        for request in history.prior_user_requests:
            row = messages[request.history_index]
            if COMPOSER_HISTORY_USER_MESSAGE_ID_KEY not in row:
                raise AuditIntegrityError("persisted human chat history is missing its message id")
            inputs.append(
                chat_ingress_input(row[COMPOSER_HISTORY_USER_MESSAGE_ID_KEY], request.content, own_compartment_id=own_compartment_id)
            )
    if user_message_id is not None:
        inputs.append(chat_ingress_input(user_message_id, message, own_compartment_id=own_compartment_id))
    return inputs


# Task 6 Step 3 (elspeth-bff8fe6864, belt-and-braces): once a genuine repair
# tool call lands following a FLAGGED END advisor pass, elide the injected
# advisor sign-off message from ``llm_messages`` so no later model call in
# the same compose() request — including the eventual CLEAN finalize turn —
# can anchor its reply on advisor findings the real user never saw. This is
# additional to (not a replacement for) the user-facing output-contract
# clause baked into the injected message itself (Steps 1-2). A single flag
# so the mechanism can be reverted independently without touching the
# threading that carries the injection index.
_ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE: Final[bool] = True


@dataclass(frozen=True, slots=True)
class _ClassifierCompletionContext:
    """Live original inputs shared by inline and retained classifier END."""

    dispatch: _DispatchOutcome
    persist: _PersistOutcome | ColdPostBatchPersistData
    turn_evidence: TurnEvidence
    recorder: BufferingRecorder
    progress: ComposerProgressSink | None
    message: str
    initial_version: int
    deadline: float
    runtime_preflight_cache: _RuntimePreflightCache
    session_scope: str
    session_id: str | None
    user_id: str | None
    mutation_success_seen: bool
    repair_turns_used: int
    composition_turns_used: int
    discovery_turns_used: int
    advisor_checkpoint_passes_used: int
    session_operation_context: SessionOperationContext | None
    plugin_snapshot: PluginAvailabilitySnapshot | None
    advisor_review_state: _AdvisorReviewState | None
    completion_gates: CompletionGateFacts | None
    provider_owner: ProviderInvocationOwner | None
    completion_capture: _ClassifyEndInvocationCapture | None
    checkpoint_phase_owner: CheckpointPhaseOwner | None = None
    checkpoint_pending: ClassifyPostPrimary | None = None


class ComposerServiceImpl:
    """LLM-driven pipeline composer with bounded composition and discovery caching.

    The request deadline bounds the tool-use loop; composition turns have
    an additional mutation limit. Discovery turns are counted for diagnostics
    without an aggregate termination cap. Cacheable discovery results are cached
    per-compose-call in a local dict (not an instance field) to avoid
    concurrent-request races.

    Budget classification: a turn containing at least one mutation tool
    call charges the composition budget. A turn containing only discovery
    tool calls increments the diagnostic discovery counter. Cache hits do not
    increment either counter.

    Args:
        catalog: CatalogService for discovery tool delegation.
        settings: ComposerSettings with composer_max_composition_turns,
            composer_timeout_seconds,
            composer_model, data_dir.
    """

    def __init__(
        self,
        catalog: CatalogService,
        settings: ComposerSettings,
        *,
        sessions_service: SessionServiceProtocol | None = None,
        session_engine: Engine | None = None,
        secret_service: WebSecretResolver | None = None,
        blob_service: BlobServiceProtocol | None = None,
        runtime_preflight_coordinator: RuntimePreflightCoordinator | None = None,
        plugin_snapshot_factory: Callable[[str], PluginAvailabilitySnapshot] | None,
        operator_profile_registry: OperatorProfileRegistry | None,
        _composition_root: object | None = None,
    ) -> None:
        trained_operator_mode = _composition_root is _TRAINED_OPERATOR_COMPOSITION_ROOT
        if plugin_snapshot_factory is None:
            raise TypeError("plugin_snapshot_factory must be provided")
        if operator_profile_registry is None and not trained_operator_mode:
            raise TypeError("operator_profile_registry must be provided")
        self._sessions_service = sessions_service
        self._policy_context = PluginPolicyContextFactory(
            catalog,
            plugin_snapshot_factory,
            operator_profile_registry,
            trained_operator_mode=trained_operator_mode,
        )
        self._chargeable_admission = ComposerChargeableAdmission(sessions_service)
        self._model = settings.composer_model
        # The compose loop's tool-contract dialect, resolved once from the
        # settings and the process environment (S1 T8, D20; the boot probe
        # resolves through the same helper). ``_planner_dialect`` is the single
        # resolution point for the compose loop: the loop builds the list it
        # sends and decode reads the dialect from this one value.
        tool_contract = resolve_composer_tool_contract(settings, env=os.environ)
        self._planner_dialect = tool_contract.planner.dialect
        loop_tools = composer_loop_tool_definitions(self._planner_dialect)
        self._tool_contract_summary = ComposerToolContractSummary(
            contract=tool_contract,
            loop_strict_tool_count=sum(1 for tool in loop_tools if "strict" in tool["function"] and tool["function"]["strict"] is True),
            loop_tool_count=len(loop_tools),
        )
        # Operator-side only (ruling 2): closed values and counts, never a
        # URL or an env value (D24).
        slog.info(
            "composer_tool_contract_resolved",
            setting=tool_contract.setting,
            planner_transport=tool_contract.planner.resolution.transport.value,
            planner_dialect=tool_contract.planner.dialect.value,
            planner_diagnostic=_diagnostic_value(tool_contract.planner.resolution.diagnostic),
            loop_strict_tool_count=self._tool_contract_summary.loop_strict_tool_count,
            loop_tool_count=self._tool_contract_summary.loop_tool_count,
        )
        # Boot advisory only — the litellm registry has known gaps (see
        # elspeth.web.composer.reasoning), so a False here is a log line for
        # operators, never a gate.
        warn_if_not_reasoning_capable(
            model=settings.composer_model,
            role="primary",
            effort=settings.composer_candidate_reasoning_effort,
        )
        warn_if_not_reasoning_capable(
            model=settings.composer_advisor_model,
            role="advisor",
            effort=settings.composer_advisor_reasoning_effort,
        )
        # Endpoint affordance (Phase 3 Task 2): resolved once here, not
        # re-derived per call. The bearer is unwrapped from SecretStr exactly
        # at this boundary and held only as a plain attribute on this
        # non-dataclass instance (default object repr does not print
        # instance attributes), never logged, never placed in an audit
        # record. None (both endpoint and key unset) means every kwargs
        # dict built below stays byte-identical to pre-affordance behaviour.
        self._endpoint_base_url: str | None = settings.composer_endpoint_base_url
        self._endpoint_api_key: str | None = (
            settings.composer_endpoint_api_key.get_secret_value() if settings.composer_endpoint_api_key is not None else None
        )
        self._advisor_endpoint_base_url: str | None = settings.composer_advisor_endpoint_base_url
        self._advisor_endpoint_api_key: str | None = (
            settings.composer_advisor_endpoint_api_key.get_secret_value()
            if settings.composer_advisor_endpoint_api_key is not None
            else None
        )
        self._provider_gateway = ProviderGateway(
            model=self._model,
            settings=settings,
            endpoint_base_url=self._endpoint_base_url,
            endpoint_api_key=self._endpoint_api_key,
        )
        self._advisor_prompt_guard_mount = build_prompt_guard_mount(settings.composer_advisor_prompt_guard)
        self._max_composition_turns = settings.composer_max_composition_turns
        self._timeout_seconds = settings.composer_timeout_seconds
        self._data_dir: str = str(settings.data_dir)
        self._session_engine = session_engine
        self._secret_service = secret_service
        # Server-authored secret→destination allowlist (elspeth-f3c1aafd25);
        # deny-by-default when the deployment configures no rules.
        self._secret_wiring_policy = runtime_secret_wiring_policy(settings.secret_wiring_allowlist)
        self._settings = settings
        advisor_provider = infer_provider_from_model_name(settings.composer_advisor_model) or infer_provider_from_unprefixed_model_name(
            settings.composer_advisor_model
        )
        if advisor_provider is None:
            raise ValueError(
                "composer_advisor_model provider could not be inferred; use a provider-prefixed model name "
                "or a recognized OpenAI/Anthropic model name"
            )
        self._advisor_provider = advisor_provider
        self._preflight = ComposerPreflight(
            catalog=catalog,
            settings=settings,
            policy_context=self._policy_context,
            blob_service=blob_service,
            secret_service=secret_service,
            secret_wiring_policy=self._secret_wiring_policy,
            operator_profile_registry=operator_profile_registry,
            coordinator=runtime_preflight_coordinator or RuntimePreflightCoordinator(),
        )
        self._availability = compute_availability(
            model=self._model,
            advisor_model=settings.composer_advisor_model,
            advisor_provider=self._advisor_provider,
            endpoint_base_url=self._endpoint_base_url,
            endpoint_api_key=self._endpoint_api_key,
            advisor_endpoint_base_url=self._advisor_endpoint_base_url,
            advisor_endpoint_api_key=self._advisor_endpoint_api_key,
        )
        missing_guard_roles = tuple(
            role
            for role, mount in (
                ("composer", self._provider_gateway.prompt_guard_mount),
                ("advisor", self._advisor_prompt_guard_mount),
            )
            if mount is None
        )
        if settings.composer_prompt_guard_mode == "required" and missing_guard_roles:
            self._availability = replace(
                self._availability,
                available=False,
                reason="Composer is unavailable: required prompt guard configuration is missing for "
                + ", ".join(missing_guard_roles)
                + ".",
            )
        elif settings.composer_prompt_guard_mode == "debug-when-unconfigured":
            slog.warning(
                "composer_prompt_guard_debug_mode", unconfigured_roles=missing_guard_roles, mode=settings.composer_prompt_guard_mode
            )
        from elspeth.web.composer.redaction_telemetry import OtelRedactionTelemetry
        from elspeth.web.sessions.telemetry import build_sessions_telemetry

        self._max_tool_calls_per_turn: int = self._settings.composer_max_tool_calls_per_turn
        self._telemetry: _SessionsTelemetry = build_sessions_telemetry(meter=metrics.get_meter("elspeth.web.composer"))
        self._redaction_telemetry: RedactionTelemetry = OtelRedactionTelemetry()

        # F-5a. Re-read both static prompt source files and compare them with
        # the individually cached hashes. The audit hash covers the exact
        # composed prompt, while these checks make drift in either source an
        # operator-actionable Tier-1 anomaly requiring restart.
        from elspeth.web.composer.prompts import (
            PIPELINE_CAPABILITIES_SKILL_HASH,
            PIPELINE_CAPABILITIES_SKILL_NAME,
            PIPELINE_COMPOSER_INTERACTION_SKILL_HASH,
            PIPELINE_COMPOSER_SKILL_NAME,
        )

        assert_skill_hash_unchanged_on_disk(
            PIPELINE_COMPOSER_SKILL_NAME,
            PIPELINE_COMPOSER_INTERACTION_SKILL_HASH,
        )
        assert_skill_hash_unchanged_on_disk(
            PIPELINE_CAPABILITIES_SKILL_NAME,
            PIPELINE_CAPABILITIES_SKILL_HASH,
        )
        # Bind the service instance to the exact prompt stack it will send.
        # This includes the deployment overlay and is rendered once so a
        # mid-service file change cannot split provider bytes from identity or
        # archival evidence.
        self._composer_skill_text: str = render_system_prompt(self._data_dir)
        self._composer_skill_hash: str = hashlib.sha256(self._composer_skill_text.encode("utf-8")).hexdigest()
        self._session_tools = SessionToolOwner(
            sessions_service=self._sessions_service,
            telemetry=self._telemetry,
            per_term_cap=self._settings.composer_interpretation_rate_limit_per_term,
            per_session_day_cap=self._settings.composer_interpretation_rate_limit_per_session_day,
            model_identifier=self._model,
            provider=self._availability.provider,
            composer_skill_hash=self._composer_skill_hash,
        )
        self._advisor_checkpoint = AdvisorCheckpointOwner(
            settings=settings,
            composer_skill_text=self._composer_skill_text,
            advisor_endpoint_base_url=self._advisor_endpoint_base_url,
            advisor_endpoint_api_key=self._advisor_endpoint_api_key,
            sessions_service=sessions_service,
            chargeable_admission=self._chargeable_admission,
            prompt_guard_mount=self._advisor_prompt_guard_mount,
            secret_service=secret_service,
        )
        self._composer_skill_name: str = PIPELINE_COMPOSER_SKILL_NAME
        self._interpretation_surfacing = InterpretationSurfacing(
            sessions_service=sessions_service,
            per_term_cap=settings.composer_interpretation_rate_limit_per_term,
            per_session_day_cap=settings.composer_interpretation_rate_limit_per_session_day,
            model_identifier=self._model,
            provider=self._availability.provider or "unknown",
            composer_skill_hash=self._composer_skill_hash,
        )
        self._completion = CompositionCompletion(
            sessions_service=sessions_service,
            session_engine=session_engine,
            data_dir=self._data_dir,
            preflight=self._preflight,
            advisor_checkpoint=self._advisor_checkpoint,
            interpretation_surfacing=self._interpretation_surfacing,
            max_advisor_checkpoint_passes=settings.composer_advisor_checkpoint_max_passes,
        )
        self._schema_disclosure = SchemaDisclosureTracker()
        # F-5c gate: ensures the first ``compose()`` call upserts
        # the skill markdown into ``skill_markdown_history`` exactly once
        # per service instance. Subsequent compose() calls observe the
        # flag set and skip the upsert.
        self._skill_markdown_history_upserted: bool = False

    @property
    def composer_prompt_guard_mount(self) -> PromptGuardMount | None:
        """Deployment-owned primary mount shared with the title producer."""
        return self._provider_gateway.prompt_guard_mount

    @property
    def tool_contract_summary(self) -> ComposerToolContractSummary:
        """Both routes' resolved tool contract and the compose loop's effective strict count.

        Operator-side and test-facing only: it is never published on the
        unauthenticated status surface (D14, ruling 2).
        """
        return self._tool_contract_summary

    @classmethod
    def for_trained_operator(
        cls,
        catalog: CatalogService,
        settings: ComposerSettings,
        **kwargs: Any,
    ) -> ComposerServiceImpl:
        """Explicit non-web composition root with unrestricted local catalog access."""
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        return cls(
            catalog=catalog,
            settings=settings,
            plugin_snapshot_factory=lambda _user_id: snapshot,
            operator_profile_registry=None,
            _composition_root=_TRAINED_OPERATOR_COMPOSITION_ROOT,
            **kwargs,
        )

    def _require_sessions_service(self) -> SessionServiceProtocol:
        """Return the wired sessions service or fail at the persistence boundary."""

        if self._sessions_service is None:
            raise RuntimeError("sessions_service not wired")
        return self._sessions_service

    async def _maybe_upsert_skill_markdown_history(self) -> None:
        """Best-effort first-use upsert of the composer skill markdown (F-5c).

        On the first ``_compose_loop`` entry of this service instance,
        archive the exact skill markdown text into
        ``skill_markdown_history`` keyed by SHA-256. Subsequent calls are
        a cheap in-process branch (flag check) and never touch the DB.

        No-op when ``sessions_service`` is not wired (CLI / unit-test
        paths) — the upsert is meaningful only on deployments that
        persist interpretation events. Per-instance flag, not per-process:
        a service rebuild (test fixture, lifespan restart) re-runs the
        upsert on the new instance, which is harmless under
        ``INSERT OR IGNORE``.

        Failures are NOT silenced: the upsert is best-effort with
        respect to the audit-event row (we don't gate the interpretation
        write on it succeeding), but a real DB failure here indicates
        the session DB is unreachable, in which case the broader compose
        loop is also unable to function — letting the exception escape
        surfaces the failure at the start of the request instead of
        midway through.
        """
        if self._skill_markdown_history_upserted:
            return
        if self._sessions_service is None:
            return
        # Archive the exact service-instance prompt, including its deployment
        # overlay, not either static source markdown in isolation.
        text = self._composer_skill_text
        sha256_hex = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Defensive Tier-1 consistency check: the service's composed prompt
        # hash and the exact archived content MUST agree.
        if sha256_hex != self._composer_skill_hash:
            raise RuntimeError(
                f"Composer skill hash drift detected: service instance cached "
                f"{self._composer_skill_hash!r} but its retained prompt bytes hash to "
                f"{sha256_hex!r}. Restart elspeth-web.service so the in-memory skill "
                f"prompt and the audit row's composer_skill_hash agree."
            )
        await self._sessions_service.upsert_skill_markdown_history(
            skill_hash=sha256_hex,
            filename=f"{self._composer_skill_name}.md",
            content=text,
        )
        self._skill_markdown_history_upserted = True

    def get_availability(self) -> ComposerAvailability:
        """Return the boot-time composer availability snapshot."""
        return self._availability

    async def explain_run_diagnostics(
        self,
        snapshot: Mapping[str, object],
        *,
        recorder: BufferingRecorder | None = None,
        session_operation_context: SessionOperationContext | None = None,
        provider_custody: ProviderCallCustody | None = None,
    ) -> str:
        """Return a plain-language explanation of a bounded run snapshot.

        The explanation is advisory UI text only: it does not call composer
        tools, mutate CompositionState, or persist chat messages.
        """
        if recorder is None:
            recorder = BufferingRecorder()
        if not self._availability.available:
            raise ComposerServiceError(self._availability.reason or "Composer is unavailable.")

        await self._chargeable_admission.require(session_operation_context)
        with composer_quota_scope(self._require_sessions_service(), session_operation_context), prompt_guard_user_scope(""):
            try:
                messages = build_run_diagnostics_messages(snapshot, data_dir=self._data_dir)
            except OSError as exc:
                raise ComposerServiceError(f"Failed to load deployment skill ({type(exc).__name__})") from exc

            try:
                from litellm.exceptions import APIError as LiteLLMAPIError
                from litellm.exceptions import (
                    BlockedPiiEntityError,
                    BudgetExceededError,
                    GuardrailRaisedException,
                )

                return await self._provider_gateway._call_text_llm_with_audit(
                    messages,
                    timeout=self._settings.composer_sync_timeout_seconds,
                    recorder=recorder,
                    provider_custody=provider_custody,
                )
            except TimeoutError:
                raise ComposerServiceError("Run diagnostics explanation timed out") from None
            except (
                LiteLLMAPIError,
                BudgetExceededError,
                BlockedPiiEntityError,
                GuardrailRaisedException,
            ) as exc:
                raise ComposerServiceError(f"LLM unavailable ({type(exc).__name__})") from exc

    def _terminal_recovery_result(
        self,
        completion: _AdmittedLLMCompletion,
        state: CompositionState,
        recorder: BufferingRecorder,
        *,
        prior_tools_persisted: bool = False,
    ) -> ComposerResult | None:
        try:
            intent = parse_recovery_tool_batch(completion.tool_batch)
        except (ValueError, ComposerServiceError) as exc:
            attach_llm_calls(exc, recorder)
            raise
        if intent is None:
            return None
        return ComposerResult(
            message=intent.message,
            state=state,
            graph_recovery_intent=intent,
            tool_invocations=() if prior_tools_persisted else recorder.invocations,
            llm_calls=recorder.llm_calls,
        )

    def _enforce_tool_call_cap(
        self,
        *,
        assistant_tool_calls: Sequence[_AdmittedToolCall],
        state: CompositionState,
        initial_version: int,
        composition_turns_used: int,
        discovery_turns_used: int,
        recorder: BufferingRecorder,
        failed_turn: FailedTurnMetadata | None,
        persisted_tool_call_turn: bool,
    ) -> None:
        """Reject an admitted completion whose tool batch exceeds the shared cap."""

        observed = len(assistant_tool_calls)
        if observed <= self._max_tool_calls_per_turn:
            return
        self._telemetry.tool_call_cap_exceeded_total.add(1)
        raise ComposerConvergenceError.capture(
            max_turns=composition_turns_used + discovery_turns_used,
            budget_exhausted="composition",
            state=state,
            initial_version=initial_version,
            tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
            llm_calls=recorder.llm_calls,
            reason="tool_call_cap_exceeded",
            evidence={
                "observed": observed,
                "cap": self._max_tool_calls_per_turn,
            },
            failed_turn=failed_turn,
        )

    def _prepare_primary_delivery(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        recorder: BufferingRecorder,
        turn_evidence: TurnEvidence | None,
        state: CompositionState,
        failed_turn: FailedTurnMetadata | None,
        reply_only: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], BlobDeliveryAttempt | None, PreparedProviderInput]:
        """Freeze the selected input and derive the gateway's exact marked hash."""
        delivery = None
        if turn_evidence is not None:
            turn_evidence.transcript.observe_messages(messages)
        try:
            if reply_only:
                prepared, framed = self._provider_gateway.prepare_reply_input(
                    messages,
                    tools,
                    instruction=reply_instruction_message(),
                    prefix=_REPLY_ONLY_HISTORY_PREFIX,
                    suffix=_REPLY_ONLY_HISTORY_SUFFIX,
                    frame_one=lambda message: _reply_only_messages((message,))[0],
                    frame_all=lambda: _reply_only_messages(messages, turn_evidence=turn_evidence),
                )
                if turn_evidence is not None:
                    turn_evidence.transcript.observe_messages(framed)
            else:
                prepared = self._provider_gateway.prepare_loop_input(messages, tools)
        except ComposerContextUnsupported as refused:
            # This owner now prepares before the physical audit/deadline
            # wrapper. Retain its existing persisted-state baseline and exact
            # physical calls without replaying completed tool effects.
            raise refused.with_evidence(
                state=state, initial_version=state.version, llm_calls=recorder.llm_calls, failed_turn=failed_turn
            ) from refused
        except (ComposerAdmissionRefused, AuditIntegrityError, TypeError, ValueError, RecursionError) as refused:
            attach_llm_calls(refused, recorder)
            raise
        selected_messages = prepared.evidence.messages
        selected_tools = prepared.tools if prepared.tools is not None else []
        if turn_evidence is not None:
            delivery = turn_evidence.prepare_delivery(
                selected_messages,
                previous_calls=recorder.llm_calls,
                tools=prepared.tools,
                scope="primary",
            )
        return selected_messages, selected_tools, delivery, prepared

    async def _persist_turn_audit(
        self,
        *,
        tool_outcomes: tuple[_ToolOutcome, ...],
        mutation_ceiling: ComposerMutationCeiling | None,
        authority_revocation: ComposerMutationRevocation | None,
        base_state: CompositionState,
        decoded_args_by_call_id: Mapping[str, Mapping[str, Any]],
        private_authorings: tuple[PrivateToolAuthoring, ...],
        private_invocations: tuple[ComposerToolInvocation, ...],
        assistant_message: _AdmittedAssistantMessage,
        raw_assistant_content: str | None,
        assistant_tool_calls: tuple[_AdmittedToolCall, ...],
        crash_pending: bool,
        session_id: str | None,
        session_operation_context: SessionOperationContext | None = None,
        current_state_id: str | None,
        persisted_tool_call_turn: bool,
        persisted_assistant_message_id: str | None,
        # REQUIRED (no default): the content of the row named by
        # ``persisted_assistant_message_id``. Threading the id without it is the
        # shape that silently regresses to re-emitting already-persisted prose
        # (elspeth-d581b3da7f), so a missed site must fail loudly here rather
        # than default to None.
        persisted_assistant_content: str | None,
        ingress: CompositionIngressRecord | None = None,
        chat_ingress_inputs: list[ChatIngressInput] | None = None,
        advisor_repair_context_introduced: bool = False,
        required_work: RequiredWorkBinding | None = None,
        turn_evidence: TurnEvidence | None = None,
        continuation: ContinuationLiveP4 | None = None,
        terminal_completion: TerminalServiceCompletion | None = None,
    ) -> _PersistOutcome:
        """Phase P4 of the compose loop — delegates to :func:`turn_audit.persist_turn_audit`."""
        from elspeth.web.composer.turn_audit import persist_turn_audit

        persisted_assistant_message = assistant_message
        persisted_raw_assistant_content = raw_assistant_content
        if advisor_repair_context_introduced:
            # The assistant row below holds the fixed status line, so the
            # turn's own prose is kept as a non-rendered audit row first.
            await self._completion._persist_withheld_reply(
                "advisor_repair_tool_turn",
                assistant_message.content or "",
                session_id=session_id,
                session_operation_context=session_operation_context,
            )
            persisted_assistant_message = _AdmittedAssistantMessage(
                content=_ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE,
            )
            persisted_raw_assistant_content = None

        return await persist_turn_audit(
            sessions_service=self._sessions_service,
            redaction_telemetry=self._redaction_telemetry,
            tool_outcomes=tool_outcomes,
            mutation_ceiling=mutation_ceiling,
            authority_revocation=authority_revocation,
            base_state=base_state,
            decoded_args_by_call_id=decoded_args_by_call_id,
            private_authorings=private_authorings,
            private_invocations=private_invocations,
            assistant_message=persisted_assistant_message,
            raw_assistant_content=persisted_raw_assistant_content,
            assistant_tool_calls=assistant_tool_calls,
            crash_pending=crash_pending,
            session_id=session_id,
            session_operation_context=session_operation_context,
            current_state_id=current_state_id,
            persisted_tool_call_turn=persisted_tool_call_turn,
            persisted_assistant_message_id=persisted_assistant_message_id,
            persisted_assistant_content=persisted_assistant_content,
            ingress=ingress,
            chat_ingress_inputs=chat_ingress_inputs,
            assistant_row_uses_current_dispatch=not advisor_repair_context_introduced,
            required_work=required_work,
            turn_evidence=turn_evidence,
            continuation=continuation,
            terminal_completion=terminal_completion,
        )

    async def _classify_and_budget_turn(
        self,
        *,
        dispatch: _DispatchOutcome,
        turn_evidence: TurnEvidence,
        persist: _PersistOutcome | ColdPostBatchPersistData,
        llm_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        recorder: BufferingRecorder,
        anti_anchor: AntiAnchorTracker,
        progress: ComposerProgressSink | None,
        message: str,
        initial_version: int,
        deadline: float,
        runtime_preflight_cache: _RuntimePreflightCache,
        session_scope: str,
        session_id: str | None,
        user_id: str | None,
        mutation_success_seen: bool,
        repair_turns_used: int,
        composition_turns_used: int,
        discovery_turns_used: int,
        advisor_checkpoint_passes_used: int,
        session_operation_context: SessionOperationContext | None = None,
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        advisor_review_state: _AdvisorReviewState | None = None,
        # Durable advisor gate fact from the prior state row (ruling
        # 2026-09-22). ``None`` = none known: the END gate reviews as before.
        completion_gates: CompletionGateFacts | None = None,
        provider_owner: ProviderInvocationOwner | None = None,
        classifier_primary_issuer: ClassifierPrimaryIssuer | None = None,
        completion_capture: _ClassifyEndInvocationCapture | None = None,
    ) -> (
        _ClassifyOutcome
        | ClassifyPostPrimary
        | ClassifySourceProofStop
        | ClassifierReplyDeadline
        | VerifiedClassifierPaidOverflowTerminal
        | ClassifierNoSdkRefusalReceipt
    ):
        """Phase P5 of the compose loop — anti-anchor + turn accounting.

        Three concerns share this phase because their decision flow is
        sequential:

        1. **Anti-anchor hint (§7.7).** When the last three failed tool
           calls share the same (tool_name, arguments_hash), inject a
           role="user" hint into ``llm_messages`` so the model breaks
           the anchored retry. The hint is persisted via the normal
           ``chat_messages`` path.
        2. **Cache-hit short-circuit.** A batch of discovery cache hits
           does not increment either turn counter: continue.
        3. **Turn accounting.** Charge composition or count discovery.
           Composition exhaustion gets the B-4D-3 last-chance call. Discovery
           continues within the request deadline without an aggregate turn cap.
           Advisor-only turns return to the driver without charging.

        Returns:
            ``_ClassifyOutcome(action="continue", composition_turns_delta=...,
            discovery_turns_delta=...)`` on the normal path, or
            ``_ClassifyOutcome(action="return", result=...)`` when the
            B-4D-3 bonus call terminated the loop. Composition exhaustion
            without bonus success leaves through the exception channel.
        """
        state = dispatch.state
        last_runtime_preflight = dispatch.last_runtime_preflight
        turn_has_mutation = dispatch.turn_has_mutation
        turn_has_discovery = dispatch.turn_has_discovery
        all_cache_hits = dispatch.all_cache_hits
        persisted_tool_call_turn = persist.persisted_tool_call_turn
        failed_turn = persist.failed_turn
        completion_context = _ClassifierCompletionContext(
            dispatch=dispatch,
            persist=persist,
            turn_evidence=turn_evidence,
            recorder=recorder,
            progress=progress,
            message=message,
            initial_version=initial_version,
            deadline=deadline,
            runtime_preflight_cache=runtime_preflight_cache,
            session_scope=session_scope,
            session_id=session_id,
            user_id=user_id,
            mutation_success_seen=mutation_success_seen,
            repair_turns_used=repair_turns_used,
            composition_turns_used=composition_turns_used,
            discovery_turns_used=discovery_turns_used,
            advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
            session_operation_context=session_operation_context,
            plugin_snapshot=plugin_snapshot,
            advisor_review_state=advisor_review_state,
            completion_gates=completion_gates,
            provider_owner=provider_owner,
            completion_capture=completion_capture,
        )

        # P4 and original failure/cancellation primacy have completed. Reuse
        # the current preview's observation without resolving the blob again:
        # newly ready bytes cannot reopen this request's primary dialogue.
        source_stop: ProofUnavailable | MalformedSourceProofClaim | None = None
        source_stop_origin: Literal["preview", "source_assessment"] | None = None
        if dispatch.tool_outcomes:
            terminal = dispatch.tool_outcomes[-1]
            if (
                terminal.call.function.name == "preview_pipeline"
                and terminal.error_class is None
                and isinstance(terminal.response, ToolResult)
            ):
                source_stop = preview_source_proof_stop(admit_discovery_result("preview_pipeline", terminal.response), state)
                if source_stop is not None:
                    source_stop_origin = "preview"
        terminal_without_preview = _tool_batch_staged_terminal_interpretation_review_handoff(dispatch.tool_outcomes) or (
            turn_has_mutation and composition_turns_used + 1 >= self._max_composition_turns
        )
        if source_stop is None and terminal_without_preview:
            assessment = self._completion._assess_source_proof(
                state, session_id=session_id, session_operation_context=session_operation_context
            )
            if type(assessment) in (ProofUnavailable, MalformedSourceProofClaim):
                source_stop = cast(ProofUnavailable | MalformedSourceProofClaim, assessment)
                source_stop_origin = "source_assessment"
        if source_stop is not None:
            if classifier_primary_issuer is not None:
                from elspeth.web.composer.continuation_classifier_handoff import (
                    ClassifySourceProofStop,
                    encode_classifier_source_stop_proof,
                )
                from elspeth.web.composer.continuation_classifier_owned_source import ClassifierOwnedSourceObservation

                if completion_capture is None or source_stop_origin is None:
                    raise AuditIntegrityError("Continuation source stop lost its actual original observation owner")
                original_fence = classifier_primary_issuer.running.session_operation_context.fence
                completion_capture.record_original_owned_source(
                    ClassifierOwnedSourceObservation(
                        kind="source_proof_refusal",
                        source_checkpoint_id=classifier_primary_issuer.selected.checkpoint_id,
                        source_cursor=classifier_primary_issuer.selected.stack.cursor,
                        state_sha256=stable_hash(state.to_dict()),
                        physical_operation_id=original_fence.operation_id,
                        physical_operation_epoch=original_fence.operation_epoch,
                        proof_json=encode_classifier_source_stop_proof(source_stop),
                        proof_origin=source_stop_origin,
                        terminal_tool_call_id=dispatch.tool_outcomes[-1].call.id if source_stop_origin == "preview" else None,
                        assessment_trigger=(
                            "staged_review"
                            if _tool_batch_staged_terminal_interpretation_review_handoff(dispatch.tool_outcomes)
                            else "composition_limit"
                        )
                        if source_stop_origin == "source_assessment"
                        else None,
                        composition_limit=self._max_composition_turns
                        if source_stop_origin == "source_assessment"
                        and not _tool_batch_staged_terminal_interpretation_review_handoff(dispatch.tool_outcomes)
                        else None,
                    )
                )

                return ClassifySourceProofStop(
                    source_checkpoint_id=classifier_primary_issuer.selected.checkpoint_id,
                    source_cursor=classifier_primary_issuer.selected.stack.cursor,
                    proof_json=encode_classifier_source_stop_proof(source_stop),
                    composition_turns_used=composition_turns_used,
                    discovery_turns_used=discovery_turns_used,
                    composition_turns_delta=int(turn_has_mutation),
                    discovery_turns_delta=int(turn_has_discovery and not turn_has_mutation),
                )
            return await self._complete_classifier_source_stop(context=completion_context, proof=source_stop)

        # §7.7 anti-anchor hint: if the last 3 failed tool calls share the
        # same tool (with identical or different argument hashes), report
        # those observations without inferring whether repairs progressed.
        # Inject a synthetic role="user" hint before the next LLM turn. consume_fire()
        # clears the deque so the hint cannot re-fire on the same anchor.
        # Persisted first as a system-origin audit row whose closed envelope
        # records the provider role. Replay restores that exact role/content.
        if anti_anchor.should_fire():
            hint_text = anti_anchor.build_hint()
            if session_id is not None:
                # Audit publication is a precondition of the provider-visible
                # intervention. A storage failure propagates before the hint is
                # appended, so the model can never act on unrecorded control.
                # The row is a fenced session write (P4-D6 family A2b): it
                # carries the compose operation this turn runs under.
                if session_operation_context is None:
                    raise TypeError("compose audit hint requires the turn's session_operation_context")
                await self._require_sessions_service().add_message(
                    UUID(session_id),
                    "audit",
                    hint_text,
                    writer_principal="compose_loop",
                    tool_calls=[anti_anchor_control_envelope(hint_text)],
                    session_operation_context=session_operation_context,
                )
            anti_anchor.consume_fire()
            llm_messages.append({"role": "user", "content": hint_text})
            is_drift_hint = "with different arguments" in hint_text or "sending different arguments" in hint_text
            await emit_progress(
                progress,
                ComposerProgressEvent(
                    phase="using_tools",
                    headline="ELSPETH observed three failed calls to the same tool.",
                    evidence=(
                        (
                            "The last 3 tool calls used different arguments and failed; their error details and progress were not compared."
                            if is_drift_hint
                            else "The last 3 tool calls used identical arguments and failed."
                        ),
                        "A structural hint was injected to help the model converge.",
                    ),
                    likely_next="The model will review the actual tool results before choosing its next step.",
                ),
            )

        # If ALL tool calls in this turn were cache hits, no budget
        # charge — continue to next turn without incrementing.
        if all_cache_hits:
            return _ClassifyOutcome(action="continue")

        if _tool_batch_staged_terminal_interpretation_review_handoff(dispatch.tool_outcomes):
            # A pending interpretation review is a user-action boundary, not
            # another model-planning step. Complete the handoff after P4 has
            # persisted the tool-call turn so remaining structured review sites
            # can be surfaced against the frozen state id. This prevents the
            # model from re-surfacing the same review until the wall-clock timeout.
            # The branch is intentionally narrower than "any review tool
            # succeeded": a review followed by another tool call or a mixed
            # success/error batch is not a terminal user-action boundary.
            # Verify the graph before creating any further review cards.
            runtime_result: ValidationResult | None = last_runtime_preflight
            if state.version > initial_version:
                runtime_result = await self._preflight.cached_runtime_preflight(
                    state,
                    user_id=user_id,
                    session_id=session_id,
                    cache=runtime_preflight_cache,
                    initial_version=initial_version,
                    session_scope=session_scope,
                    llm_calls=recorder.llm_calls,
                    plugin_snapshot=plugin_snapshot,
                    session_operation_context=session_operation_context,
                )

            # Verified-handoff repair gate (elspeth-85f3cc3022, battery round
            # 8 g03-s1). The staged review is a user-action boundary only when
            # the review is genuinely all that remains: at pin 230fd9dfd this
            # exit completed the handoff over a WIRED state whose masked
            # re-validation carried an edge-contract violation — the finalize
            # tail disclosed the finding to the USER, but the MODEL never got
            # a repair turn, so a known-broken pipeline was handed to a review
            # card that cannot fix it. ``_attempt_preflight_repair`` verifies
            # the handoff claim via the masked re-validation and spends the
            # shared ``_MAX_REPAIR_TURNS`` budget on masked failures BEFORE
            # the handoff may complete — the same gate the no-tool completion
            # claim consumes. A verified pure handoff, or a spent budget,
            # falls through to the handoff exactly as before, preserving the
            # elspeth-e6ff1b8c13 bound against another authoring turn. The
            # terminal reply below cannot dispatch tools. Wiredness
            # (sources AND outputs — the cross-turn arm's applicability axis)
            # scopes the gate: an early-staged review over a half-built draft
            # is incomplete by nature, not damaged, and repair pressure there
            # would resurrect the re-surfacing spam e6ff1b8c13 fixed.
            if (
                state.sources
                and state.outputs
                and await self._completion._attempt_preflight_repair(
                    state=state,
                    llm_messages=llm_messages,
                    user_id=user_id,
                    session_id=session_id,
                    last_runtime_preflight=runtime_result,
                    runtime_preflight_cache=runtime_preflight_cache,
                    initial_version=initial_version,
                    session_scope=session_scope,
                    recorder=recorder,
                    repair_turns_used=repair_turns_used,
                    plugin_snapshot=plugin_snapshot,
                    session_operation_context=session_operation_context,
                )
            ):
                return _ClassifyOutcome(
                    action="continue",
                    composition_turns_delta=1 if turn_has_mutation else 0,
                    discovery_turns_delta=1 if turn_has_discovery else 0,
                    repair_turns_delta=1,
                )

            if state.sources and state.outputs and runtime_result is not None and _is_pending_interpretation_handoff(runtime_result):
                findings = await self._preflight.pending_handoff_outstanding_findings(
                    state,
                    user_id=user_id,
                    session_id=session_id,
                    cache=runtime_preflight_cache,
                    initial_version=initial_version,
                    session_scope=session_scope,
                    llm_calls=recorder.llm_calls,
                    plugin_snapshot=plugin_snapshot,
                    session_operation_context=session_operation_context,
                    deadline=deadline,
                )
                if findings is not None:
                    runtime_result = findings

            if runtime_result is None or runtime_result.is_valid or _is_pending_interpretation_handoff(runtime_result):
                if session_operation_context is None:
                    raise RuntimeError("pending interpretation surfacing requires the compose operation context")
                await self._interpretation_surfacing.surface_pending_interpretation_reviews(
                    state,
                    session_id=session_id,
                    current_state_id=persist.current_state_id,
                    session_operation_context=session_operation_context,
                )
                reply: _AdmittedAssistantMessage | None = None
                reply_clock_sample = asyncio.get_event_loop().time()
                remaining = deadline - reply_clock_sample
                reply_refusal_kind: Literal["reply_protocol_echo", "reply_deadline_exhausted"] | None = (
                    "reply_deadline_exhausted" if remaining <= 0 else None
                )
                if remaining <= 0 and classifier_primary_issuer is not None:
                    from elspeth.web.composer.continuation_classifier_handoff import encode_classifier_runtime_preflight
                    from elspeth.web.composer.continuation_classifier_owned_source import ClassifierOwnedSourceObservation

                    original_fence = classifier_primary_issuer.running.session_operation_context.fence
                    # This is the actual reached no-SDK boundary. Return to
                    # the driver synchronously; the expired action cannot
                    # prepare a request, finalize, or capture runtime owners.
                    return classifier_primary_issuer.seal_reply_deadline(
                        ClassifierOwnedSourceObservation(
                            kind="reply_deadline_exhausted",
                            source_checkpoint_id=classifier_primary_issuer.selected.checkpoint_id,
                            source_cursor=classifier_primary_issuer.selected.stack.cursor,
                            state_sha256=stable_hash(state.to_dict()),
                            physical_operation_id=original_fence.operation_id,
                            physical_operation_epoch=original_fence.operation_epoch,
                            deadline=deadline,
                            observed_monotonic=reply_clock_sample,
                            clock_started_monotonic=classifier_primary_issuer.clock.started_monotonic,
                            clock_policy_json=canonical_json(classifier_primary_issuer.clock.policy.model_dump(mode="json")),
                        ),
                        runtime_preflight_json=encode_classifier_runtime_preflight(runtime_result),
                    )
                # A reply with no advertised tools cannot carry protocol tool
                # blocks on providers such as Bedrock Converse. Preserve each
                # complete historical record as attributed text instead; the
                # planning/audit history stays unchanged and no tool is enabled.
                provider_failures: tuple[type[Exception], ...] = (TimeoutError, _BadRequestLLMError, *advisor_provider_failure_types())
                selected_reply_messages, selected_reply_tools, reply_delivery, reply_input = self._prepare_primary_delivery(
                    messages=llm_messages,
                    tools=recovery_tool_definitions(self._planner_dialect),
                    recorder=recorder,
                    turn_evidence=turn_evidence,
                    state=state,
                    failed_turn=failed_turn,
                    reply_only=True,
                )
                if remaining > 0:
                    await emit_progress(
                        progress,
                        ComposerProgressEvent(
                            phase="calling_model",
                            headline="I'm asking the model to prepare your reply.",
                            evidence=("Review cards are staged; the model is preparing an explanation without changing the pipeline.",),
                            likely_next="ELSPETH will save the reply and show the current review and validation status.",
                        ),
                    )
                    try:
                        if classifier_primary_issuer is None:
                            completion = await self._provider_gateway._call_llm_with_audit(
                                selected_reply_messages,
                                selected_reply_tools,
                                timeout=remaining,
                                recorder=recorder,
                                provider_custody=self._new_primary_custody(provider_owner),
                                prepared_input=reply_input,
                            )
                        else:
                            issued = await classifier_primary_issuer.invoke(
                                branch="staged_review", prepared_input=reply_input, recorder=recorder, deadline=deadline
                            )
                            from elspeth.web.sessions.continuation_classifier_primary import VerifiedClassifierPaidOverflowTerminal
                            from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt

                            if isinstance(issued, (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt)):
                                if type(issued) not in (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt):
                                    raise AuditIntegrityError("classifier PRIMARY returned a non-nominal terminal")
                                return issued
                            completion = issued.completion
                    except provider_failures:
                        if classifier_primary_issuer is not None:
                            # An entered physical attempt owns its original
                            # failure; this branch cannot hide it and dispatch
                            # an END call under the classifier's action clock.
                            raise
                        # The call audit retains the failure; a trusted notice
                        # below reports the unavailable reply to the user.
                        reply = None
                    else:
                        recovery_result = self._terminal_recovery_result(
                            completion, state, recorder, prior_tools_persisted=persisted_tool_call_turn
                        )
                        if recovery_result is not None:
                            classified = _ClassifyOutcome(action="return", result=recovery_result)
                            if completion_capture is not None:
                                completion_capture.record_original_owned_terminal("owned_recovery", classified)
                            return classified
                        reply = completion.message
                        if _reply_only_protocol_echo(
                            reply.content or "",
                            history_has_tool_protocol=any(row["role"] == "tool" or "tool_calls" in row for row in llm_messages),
                            user_message=message,
                        ):
                            reply = None
                            reply_refusal_kind = "reply_protocol_echo"
                        if reply is not None and not completion.tool_batch.calls and reply_delivery is not None:
                            turn_evidence.confirm_delivery(reply_delivery, recorder.llm_calls)
                if classifier_primary_issuer is not None and reply is not None:
                    if completion.tool_batch.calls:
                        raise AuditIntegrityError("classifier handoff requires its actual committed no-tool reply")
                    from elspeth.web.composer.continuation_classifier_handoff import (
                        ClassifyPostPrimary,
                        encode_classifier_runtime_preflight,
                    )

                    original = classifier_primary_issuer.result
                    if original is None or original.completion is not completion:
                        raise AuditIntegrityError("classifier handoff lost its original physical reply")
                    return ClassifyPostPrimary(
                        branch="staged_review",
                        source_checkpoint_id=original.source_checkpoint_id,
                        source_cursor=original.source_cursor,
                        primary_effect_key=original.effect_key,
                        primary_input_sha256=original.input_sha256,
                        response=original.response.reference,
                        runtime_preflight_json=encode_classifier_runtime_preflight(runtime_result),
                        composition_turns_used=composition_turns_used,
                        discovery_turns_used=discovery_turns_used,
                        composition_turns_delta=int(turn_has_mutation),
                        discovery_turns_delta=int(turn_has_discovery),
                    )
                if classifier_primary_issuer is not None and reply is None:
                    from elspeth.web.composer.continuation_classifier_owned_source import ClassifierOwnedSourceObservation

                    if completion_capture is None or reply_refusal_kind is None:
                        raise AuditIntegrityError("Continuation reply refusal lost its original branch observation")
                    original_fence = classifier_primary_issuer.running.session_operation_context.fence
                    completion_capture.record_original_owned_source(
                        ClassifierOwnedSourceObservation(
                            kind=reply_refusal_kind,
                            source_checkpoint_id=classifier_primary_issuer.selected.checkpoint_id,
                            source_cursor=classifier_primary_issuer.selected.stack.cursor,
                            state_sha256=stable_hash(state.to_dict()),
                            physical_operation_id=original_fence.operation_id,
                            physical_operation_epoch=original_fence.operation_epoch,
                            deadline=deadline if reply_refusal_kind == "reply_deadline_exhausted" else None,
                            observed_monotonic=reply_clock_sample if reply_refusal_kind == "reply_deadline_exhausted" else None,
                            clock_started_monotonic=classifier_primary_issuer.clock.started_monotonic
                            if reply_refusal_kind == "reply_deadline_exhausted"
                            else None,
                            clock_policy_json=canonical_json(classifier_primary_issuer.clock.policy.model_dump(mode="json"))
                            if reply_refusal_kind == "reply_deadline_exhausted"
                            else None,
                        )
                    )
                return await self._complete_staged_classifier_reply(context=completion_context, reply=reply, runtime_result=runtime_result)

        # Classify turns. Discovery counts are diagnostic; the request deadline
        # and cancellation bound acquisition rather than a fixed page/tool count.
        # The current turn has already been executed (tool results
        # are in the message history). We increment first, then
        # check whether the budget is now exhausted. If so, we give
        # the LLM one last chance (B-4D-3) for composition.
        if turn_has_mutation:
            new_composition_turns_used = composition_turns_used + 1
            if new_composition_turns_used >= self._max_composition_turns:
                # B-4D-3 fix: give the LLM one last chance to see the
                # tool results and produce a text response.
                await emit_progress(progress, model_call_progress_event(message))
                selected_final_messages, selected_final_tools, final_delivery, final_input = self._prepare_primary_delivery(
                    messages=llm_messages, tools=tools, recorder=recorder, turn_evidence=turn_evidence, state=state, failed_turn=failed_turn
                )
                last_chance_result = await self._call_llm_before_deadline(
                    selected_final_messages,
                    selected_final_tools,
                    state,
                    initial_version,
                    deadline,
                    recorder=recorder,
                    # The relevant counter has already been charged; a timeout
                    # on the terminal call must report that same total.
                    composition_turns_used=new_composition_turns_used,
                    discovery_turns_used=discovery_turns_used,
                    failed_turn=failed_turn,
                    provider_owner=provider_owner,
                    prepared_input=final_input,
                    classifier_primary_issuer=classifier_primary_issuer,
                )
                from elspeth.web.sessions.continuation_classifier_primary import VerifiedClassifierPaidOverflowTerminal
                from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt

                if isinstance(last_chance_result, (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt)):
                    if type(last_chance_result) not in (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt):
                        raise AuditIntegrityError("classifier PRIMARY returned a non-nominal terminal")
                    return last_chance_result
                completion = last_chance_result
                recovery_result = self._terminal_recovery_result(
                    completion, state, recorder, prior_tools_persisted=persisted_tool_call_turn
                )
                if recovery_result is not None:
                    classified = _ClassifyOutcome(action="return", result=recovery_result)
                    if completion_capture is not None:
                        completion_capture.record_original_owned_terminal("owned_recovery", classified)
                    return classified
                self._enforce_tool_call_cap(
                    assistant_tool_calls=completion.tool_batch.calls,
                    state=state,
                    initial_version=initial_version,
                    composition_turns_used=new_composition_turns_used,
                    discovery_turns_used=discovery_turns_used,
                    recorder=recorder,
                    failed_turn=failed_turn,
                    persisted_tool_call_turn=persisted_tool_call_turn,
                )
                assistant_message = completion.message
                if not completion.tool_batch.calls:
                    if final_delivery is not None:
                        turn_evidence.confirm_delivery(final_delivery, recorder.llm_calls)
                    if classifier_primary_issuer is not None:
                        from elspeth.web.composer.continuation_classifier_handoff import (
                            ClassifyPostPrimary,
                            encode_classifier_runtime_preflight,
                        )

                        original = classifier_primary_issuer.result
                        if original is None or original.completion is not completion:
                            raise AuditIntegrityError("classifier handoff lost its original physical reply")
                        runtime_result = await self._preflight.turn_runtime_preflight(
                            state=state,
                            user_id=user_id,
                            session_id=session_id,
                            last_runtime_preflight=last_runtime_preflight,
                            runtime_preflight_cache=runtime_preflight_cache,
                            initial_version=initial_version,
                            session_scope=session_scope,
                            recorder=recorder,
                            plugin_snapshot=plugin_snapshot,
                            session_operation_context=session_operation_context,
                        )
                        return ClassifyPostPrimary(
                            branch="composition_last_chance",
                            source_checkpoint_id=original.source_checkpoint_id,
                            source_cursor=original.source_cursor,
                            primary_effect_key=original.effect_key,
                            primary_input_sha256=original.input_sha256,
                            response=original.response.reference,
                            runtime_preflight_json=encode_classifier_runtime_preflight(runtime_result),
                            composition_turns_used=composition_turns_used,
                            discovery_turns_used=discovery_turns_used,
                            composition_turns_delta=1,
                            discovery_turns_delta=0,
                        )
                    return await self._complete_last_chance_classifier_reply(
                        context=completion_context,
                        assistant_message=assistant_message,
                    )
                raise ComposerConvergenceError.capture(
                    max_turns=new_composition_turns_used + discovery_turns_used,
                    budget_exhausted="composition",
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                )
            return _ClassifyOutcome(action="continue", composition_turns_delta=1)
        if turn_has_discovery:
            return _ClassifyOutcome(action="continue", discovery_turns_delta=1)
        # The only non-cache tool currently handled outside the
        # discovery/mutation registries is request_advisor_hint. It
        # has its own per-compose budget above, so give the primary
        # model the returned guidance instead of charging discovery.
        return _ClassifyOutcome(action="continue")

    async def _complete_classifier_source_stop(
        self, *, context: _ClassifierCompletionContext, proof: ProofUnavailable | MalformedSourceProofClaim
    ) -> _ClassifyOutcome:
        """Resume the original source refusal without inventing a PRIMARY."""
        turn_evidence = context.turn_evidence
        recorder = context.recorder
        progress = context.progress
        message = context.message
        initial_version = context.initial_version
        deadline = context.deadline
        runtime_preflight_cache = context.runtime_preflight_cache
        session_scope = context.session_scope
        session_id = context.session_id
        user_id = context.user_id
        repair_turns_used = context.repair_turns_used
        composition_turns_used = context.composition_turns_used
        discovery_turns_used = context.discovery_turns_used
        advisor_checkpoint_passes_used = context.advisor_checkpoint_passes_used
        session_operation_context = context.session_operation_context
        plugin_snapshot = context.plugin_snapshot
        advisor_review_state = context.advisor_review_state
        completion_gates = context.completion_gates
        provider_owner = context.provider_owner
        dispatch, persist = context.dispatch, context.persist
        state, last_runtime_preflight = dispatch.state, dispatch.last_runtime_preflight
        turn_has_mutation, turn_has_discovery = dispatch.turn_has_mutation, dispatch.turn_has_discovery
        persisted_assistant_message_id = persist.persisted_assistant_message_id
        persisted_assistant_content = persist.persisted_assistant_content
        persisted_tool_call_turn = persist.persisted_tool_call_turn
        failed_turn = persist.failed_turn
        source_stop = proof
        stopped = await self._completion._terminate_source_proof(
            proof=source_stop,
            state=state,
            assistant_message=dispatch.assistant_message,
            turn_evidence=turn_evidence,
            message=message,
            session_id=session_id,
            current_state_id=persist.current_state_id,
            session_operation_context=session_operation_context,
            user_id=user_id,
            initial_version=initial_version,
            session_scope=session_scope,
            runtime_preflight_cache=runtime_preflight_cache,
            last_runtime_preflight=last_runtime_preflight,
            recorder=recorder,
            progress=progress,
            repair_turns_used=repair_turns_used,
            advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
            advisor_review_state=advisor_review_state or _AdvisorReviewState(),
            persisted_assistant_message_id=persisted_assistant_message_id,
            persisted_assistant_content=persisted_assistant_content,
            persisted_tool_call_turn=persisted_tool_call_turn,
            plugin_snapshot=plugin_snapshot,
            deadline=deadline,
            composition_turns_used=composition_turns_used + int(turn_has_mutation),
            discovery_turns_used=discovery_turns_used + int(turn_has_discovery and not turn_has_mutation),
            failed_turn=failed_turn,
            completion_gates=completion_gates,
            provider_owner=provider_owner,
            checkpoint_phase_owner=context.checkpoint_phase_owner,
        )
        classified = _ClassifyOutcome(
            action="return",
            result=stopped.result,
            composition_turns_delta=int(turn_has_mutation),
            discovery_turns_delta=int(turn_has_discovery and not turn_has_mutation),
            advisor_passes_delta=stopped.advisor_passes_delta,
            publication_binding=stopped.publication_binding,
        )
        if context.completion_capture is not None:
            context.completion_capture.record_original_owned_terminal("owned_refusal", classified, terminal=stopped)
        return classified

    async def resume_continuation_classifier_end(
        self,
        *,
        pending: ClassifyPostPrimary | ClassifySourceProofStop | ClassifyReplyRefusalReady,
        runtime: RestoredContinuationRuntime,
        dispatch: _DispatchOutcome,
        persist: _PersistOutcome | ColdPostBatchPersistData,
        primary_response: PrivateChunk | None,
        completion_capture: _ClassifyEndInvocationCapture,
        deadline: float,
        checkpoint_phase_owner: CheckpointPhaseOwner | None = None,
    ) -> _ClassifyOutcome:
        """Run the original tail after its independently proved pending cut.

        The caller admits the actual current pending source and original paid
        response in SQL. These complete DATA inputs never grant replay or a
        publication; the observer seals this invocation and SQL authenticates
        its original model pass before accepting a clean returned result.
        """
        from elspeth.web.composer.continuation_classifier_handoff import (
            ClassifyPostPrimary,
            ClassifySourceProofStop,
            decode_classifier_runtime_preflight,
            decode_classifier_source_stop_proof,
        )
        from elspeth.web.composer.continuation_classifier_owned_source import ClassifyReplyRefusalReady
        from elspeth.web.composer.continuation_codec import (
            PrivateChunk,
            decode_continuation_response,
            decode_retained_physical_audit_call,
            verify_private_chunk,
        )
        from elspeth.web.composer.continuation_finished_batch_data import ColdPostBatchPersistData
        from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
        from elspeth.web.composer.continuation_semantic_advisor_return import _ClassifyEndInvocationCapture
        from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner

        if (
            type(runtime) is not RestoredContinuationRuntime
            or type(pending) not in (ClassifyPostPrimary, ClassifySourceProofStop, ClassifyReplyRefusalReady)
            or runtime.materialization.service is not self
            or type(dispatch) is not _DispatchOutcome
            or type(persist) not in (_PersistOutcome, ColdPostBatchPersistData)
            or dispatch.state is not runtime.live.state
            or type(completion_capture) is not _ClassifyEndInvocationCapture
            or runtime.bindings.session_operation_context is None
            or runtime.bindings.required_work is None
            or runtime.bindings.provider_owner is None
            or runtime.bindings.required_work.running is not runtime.materialization.inventory.running
            or runtime.bindings.provider_owner.required_work is not runtime.bindings.required_work
            or type(deadline) not in (int, float)
            or not math.isfinite(deadline)
        ):
            raise AuditIntegrityError("classifier END changed its actual runtime, work or original batch")
        live, bindings = runtime.live, runtime.bindings
        work = bindings.required_work
        if work is None:
            raise AuditIntegrityError("classifier END lost its original required work")
        owner = work.coordinator.sql_deadline_owner
        if type(owner) is not ContinuationSQLDeadlineOwner or owner.running is not runtime.materialization.inventory.running:
            raise AuditIntegrityError("classifier END lost its original current action deadline owner")
        sampled_at = time.monotonic()
        deadline = min(deadline, bindings.deadline, sampled_at + owner.remaining_dispatch(monotonic_now=sampled_at))
        context = _ClassifierCompletionContext(
            dispatch=dispatch,
            persist=persist,
            turn_evidence=live.turn_evidence,
            recorder=live.recorder,
            progress=bindings.progress,
            message=bindings.message,
            initial_version=live.initial_version,
            deadline=deadline,
            runtime_preflight_cache=live.runtime_preflight_cache,
            session_scope=bindings.session_scope,
            session_id=bindings.session_id,
            user_id=bindings.user_id,
            mutation_success_seen=live.mutation_success_seen,
            repair_turns_used=live.repair_turns_used,
            composition_turns_used=pending.composition_turns_used,
            discovery_turns_used=pending.discovery_turns_used,
            advisor_checkpoint_passes_used=live.advisor_checkpoint_passes_used,
            session_operation_context=bindings.session_operation_context,
            plugin_snapshot=bindings.plugin_snapshot,
            advisor_review_state=live.advisor_review_state,
            completion_gates=bindings.completion_gates,
            provider_owner=bindings.provider_owner,
            completion_capture=completion_capture,
            checkpoint_phase_owner=checkpoint_phase_owner,
            checkpoint_pending=pending if type(pending) is ClassifyPostPrimary else None,
        )
        if type(pending) is ClassifyReplyRefusalReady:
            if primary_response is not None or not _tool_batch_staged_terminal_interpretation_review_handoff(dispatch.tool_outcomes):
                raise AuditIntegrityError("unpaid reply refusal changed its original staged-review source")
            return await self._complete_staged_classifier_reply(
                context=context, reply=None, runtime_result=decode_classifier_runtime_preflight(pending.runtime_preflight_json)
            )
        if type(pending) is ClassifySourceProofStop:
            if primary_response is not None or (
                pending.composition_turns_delta,
                pending.discovery_turns_delta,
            ) != (int(dispatch.turn_has_mutation), int(dispatch.turn_has_discovery and not dispatch.turn_has_mutation)):
                raise AuditIntegrityError("classifier source stop changed its no-PRIMARY branch or original charge")
            return await self._complete_classifier_source_stop(
                context=context, proof=decode_classifier_source_stop_proof(pending.proof_json)
            )
        if type(pending) is not ClassifyPostPrimary or type(primary_response) is not PrivateChunk:
            raise AuditIntegrityError("classifier END lost its original closed post-PRIMARY source")
        verify_private_chunk(pending.response, primary_response.canonical)
        if primary_response.reference != pending.response:
            raise AuditIntegrityError("classifier END changed its original physical response reference")
        completion = decode_continuation_response(primary_response)
        audit = decode_retained_physical_audit_call(primary_response)
        if (
            completion.tool_batch.calls
            or audit is None
            or tuple(call for call in live.recorder.llm_calls if call.call_id == audit.call_id) != (audit,)
        ):
            raise AuditIntegrityError("classifier END lost its exact paid no-tool response and recorder source")
        runtime_result = decode_classifier_runtime_preflight(pending.runtime_preflight_json)
        if pending.branch == "staged_review":
            if not _tool_batch_staged_terminal_interpretation_review_handoff(dispatch.tool_outcomes) or (
                pending.composition_turns_delta,
                pending.discovery_turns_delta,
            ) != (int(dispatch.turn_has_mutation), int(dispatch.turn_has_discovery)):
                raise AuditIntegrityError("classifier END changed its actual staged-review trigger")
            return await self._complete_staged_classifier_reply(context=context, reply=completion.message, runtime_result=runtime_result)
        if not dispatch.turn_has_mutation or pending.composition_turns_used + 1 < self._max_composition_turns:
            raise AuditIntegrityError("classifier END changed its actual last-chance budget trigger")
        return await self._complete_last_chance_classifier_reply(context=context, assistant_message=completion.message, retained=pending)

    async def _complete_staged_classifier_reply(
        self,
        *,
        context: _ClassifierCompletionContext,
        reply: _AdmittedAssistantMessage | None,
        runtime_result: ValidationResult | None,
    ) -> _ClassifyOutcome:
        """Run the original staged-reply tail under its actual END action."""
        turn_evidence = context.turn_evidence
        recorder = context.recorder
        progress = context.progress
        message = context.message
        initial_version = context.initial_version
        deadline = context.deadline
        runtime_preflight_cache = context.runtime_preflight_cache
        session_scope = context.session_scope
        session_id = context.session_id
        user_id = context.user_id
        repair_turns_used = context.repair_turns_used
        composition_turns_used = context.composition_turns_used
        discovery_turns_used = context.discovery_turns_used
        advisor_checkpoint_passes_used = context.advisor_checkpoint_passes_used
        session_operation_context = context.session_operation_context
        plugin_snapshot = context.plugin_snapshot
        advisor_review_state = context.advisor_review_state
        completion_gates = context.completion_gates
        provider_owner = context.provider_owner
        persist = context.persist
        state = context.dispatch.state
        persisted_assistant_message_id = persist.persisted_assistant_message_id
        persisted_assistant_content = persist.persisted_assistant_content
        persisted_tool_call_turn = persist.persisted_tool_call_turn
        failed_turn = persist.failed_turn
        advisor_gate = None
        checkpoint_caller = None
        if context.checkpoint_phase_owner is not None:
            from elspeth.web.composer.continuation_checkpoint_end_locals import IterationEndCallerLocals

            if context.checkpoint_pending is None or context.checkpoint_pending.branch != "staged_review":
                raise AuditIntegrityError("staged END checkpoint lost its original iteration continuation")
            checkpoint_caller = IterationEndCallerLocals(context.checkpoint_pending)
        if reply is not None:
            try:
                advisor_gate = await self._completion._evaluate_terminal_no_tool_advisor_gate(
                    state=state,
                    turn_evidence=turn_evidence,
                    session_operation_context=session_operation_context,
                    session_id=session_id,
                    current_state_id=persist.current_state_id,
                    assistant_message=reply,
                    llm_messages=None,
                    recorder=recorder,
                    progress=progress,
                    advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
                    repair_turns_used=repair_turns_used,
                    persisted_assistant_message_id=persisted_assistant_message_id,
                    persisted_assistant_content=persisted_assistant_content,
                    persisted_tool_call_turn=persisted_tool_call_turn,
                    allow_repair_continue=False,
                    runtime_preflight=runtime_result,
                    user_message=message,
                    user_id=user_id,
                    runtime_preflight_cache=runtime_preflight_cache,
                    initial_version=initial_version,
                    session_scope=session_scope,
                    plugin_snapshot=plugin_snapshot,
                    advisor_review_state=advisor_review_state or _AdvisorReviewState(),
                    deadline=deadline,
                    completion_gates=completion_gates,
                    provider_owner=provider_owner,
                    checkpoint_phase_owner=context.checkpoint_phase_owner,
                    checkpoint_caller=checkpoint_caller,
                )
            except _AdvisorCheckpointComposeDeadlineExpired:
                await self._completion._persist_withheld_reply(
                    "compose_deadline_expired",
                    reply.content or "",
                    session_id=session_id,
                    session_operation_context=session_operation_context,
                )
                raise ComposerConvergenceError.capture(
                    max_turns=composition_turns_used + discovery_turns_used,
                    budget_exhausted="timeout",
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                ) from None
        return await self._continue_staged_classifier_after_advisor_gate(
            context=context, reply=reply, runtime_result=runtime_result, advisor_gate=advisor_gate
        )

    async def _continue_staged_classifier_after_advisor_gate(
        self,
        *,
        context: _ClassifierCompletionContext,
        reply: _AdmittedAssistantMessage | None,
        runtime_result: ValidationResult | None,
        advisor_gate: _TerminalNoToolAdvisorGateOutcome | None,
    ) -> _ClassifyOutcome:
        """Resume this original caller after END without another PRIMARY or gate."""
        session_operation_context = context.session_operation_context
        state = context.dispatch.state
        session_id, persist = context.session_id, context.persist
        progress, recorder = context.progress, context.recorder
        initial_version, user_id = context.initial_version, context.user_id
        runtime_preflight_cache = context.runtime_preflight_cache
        session_scope, message = context.session_scope, context.message
        mutation_success_seen = context.mutation_success_seen
        repair_turns_used = context.repair_turns_used
        plugin_snapshot, deadline = context.plugin_snapshot, context.deadline
        completion_gates = context.completion_gates
        turn_has_mutation, turn_has_discovery = context.dispatch.turn_has_mutation, context.dispatch.turn_has_discovery
        persisted_assistant_message_id = persist.persisted_assistant_message_id
        persisted_assistant_content = persist.persisted_assistant_content
        persisted_tool_call_turn = persist.persisted_tool_call_turn
        if advisor_gate is not None and advisor_gate.action == "return":
            if advisor_gate.result is None:
                raise InvariantError("Staged reply review omitted its terminal result")
            result = advisor_gate.result
        else:
            result = await self._completion._surface_and_finalize_no_tools(
                session_operation_context=session_operation_context,
                assistant_message=reply if reply is not None else _AdmittedAssistantMessage(content=""),
                state=state,
                session_id=session_id,
                current_state_id=persist.current_state_id,
                progress=progress,
                recorder=recorder,
                initial_version=initial_version,
                user_id=user_id,
                last_runtime_preflight=runtime_result,
                runtime_preflight_cache=runtime_preflight_cache,
                session_scope=session_scope,
                message=message,
                mutation_success_seen=mutation_success_seen,
                repair_turns_used=repair_turns_used,
                plugin_snapshot=plugin_snapshot,
                deadline=deadline,
            )
        # ``_surface_and_finalize_no_tools`` now owns the announcement
        # (with its outstanding-findings qualification) for the
        # pending-handoff preflight shape on EVERY caller
        # (elspeth-c5350d93fd). What is left here is the residue the
        # shared tail cannot see: this branch's trigger is the TOOL
        # BATCH — ``request_interpretation_review`` succeeded and
        # terminated the batch — which is ground truth that a review was
        # staged even when the preflight was not computed this turn
        # (None), came back green, or came back red for an unrelated
        # reason. The one exclusion is the pending-handoff shape: the
        # shared tail in ``_surface_and_finalize_no_tools`` appends the
        # suffix for exactly that shape, so this predicate is its exact
        # complement — one side always announces the staged card, never
        # both. Re-appending on the pending-handoff shape would emit the
        # suffix TWICE and pass
        # ``_enforce_augmentation_prefix_invariant`` silently, since a
        # doubled suffix still keeps the prose as a strict prefix.
        #
        # Which SHAPE the announcement takes — and how it avoids
        # stacking on a suffix the tail already appended on the
        # cross-turn red arm — belongs to
        # ``_announce_staged_review_handoff`` (elspeth-2ed41f0a4a R1).
        handoff_result = (
            _announce_staged_review_handoff(
                result,
                (result.raw_assistant_content or "")
                if advisor_gate is not None and advisor_gate.action == "return"
                else reply.content
                if reply is not None
                else "",
            )
            if (result.runtime_preflight is None or not _is_pending_interpretation_handoff(result.runtime_preflight))
            else result
        )
        handoff_result = _with_advisor_gate_decision(
            handoff_result,
            completion_gates,
            result.advisor_gate_decision
            if advisor_gate is not None and advisor_gate.action == "return"
            else advisor_gate.advisor_gate_decision
            if advisor_gate is not None
            else None,
            advisor_gate.graph_review_identity if advisor_gate is not None else None,
        )
        if reply is None:
            handoff_result = replace(
                handoff_result,
                message=_no_tool_policy.compose_review_reply_unavailable_message(handoff_result.message),
                raw_assistant_content=("" if handoff_result.raw_assistant_content is None else handoff_result.raw_assistant_content),
            )
        threaded = replace(
            handoff_result,
            repair_turns_used=repair_turns_used,
            persisted_assistant_message_id=persisted_assistant_message_id,
            persisted_assistant_content=persisted_assistant_content,
            persisted_tool_call_turn=persisted_tool_call_turn,
            # The fresh reply is not P4's persisted tool-call narration.
            persisted_assistant_matches_terminal_model_turn=False,
        )
        classified = _ClassifyOutcome(
            action="return",
            result=threaded,
            composition_turns_delta=1 if turn_has_mutation else 0,
            discovery_turns_delta=1 if turn_has_discovery else 0,
            advisor_passes_delta=advisor_gate.advisor_passes_delta if advisor_gate is not None else 0,
            publication_binding=advisor_gate.publication_binding if advisor_gate is not None else None,
        )
        if context.completion_capture is not None:
            if advisor_gate is None:
                context.completion_capture.record_original_owned_terminal("owned_refusal", classified)
            else:
                terminal = _TerminateOutcome(
                    action="return",
                    result=threaded,
                    advisor_passes_delta=advisor_gate.advisor_passes_delta,
                    advisor_review_state=advisor_gate.advisor_review_state,
                    publication_binding=advisor_gate.publication_binding,
                    end_disposition=(
                        _end_disposition_after_terminal_barriers(threaded, advisor_gate)
                        if advisor_gate.action == "fall_through"
                        else _EndCompletionDisposition.BLOCKED
                    ),
                )
                context.completion_capture.record_original_model_end(terminal, classified)
        return classified

    async def _complete_last_chance_classifier_reply(
        self,
        *,
        context: _ClassifierCompletionContext,
        assistant_message: _AdmittedAssistantMessage,
        retained: ClassifyPostPrimary | None = None,
    ) -> _ClassifyOutcome:
        """Run the original last-chance tail after its paid reply settled."""
        turn_evidence = context.turn_evidence
        recorder = context.recorder
        progress = context.progress
        message = context.message
        initial_version = context.initial_version
        deadline = context.deadline
        runtime_preflight_cache = context.runtime_preflight_cache
        session_scope = context.session_scope
        session_id = context.session_id
        user_id = context.user_id
        repair_turns_used = context.repair_turns_used
        composition_turns_used = context.composition_turns_used
        discovery_turns_used = context.discovery_turns_used
        advisor_checkpoint_passes_used = context.advisor_checkpoint_passes_used
        session_operation_context = context.session_operation_context
        plugin_snapshot = context.plugin_snapshot
        advisor_review_state = context.advisor_review_state
        completion_gates = context.completion_gates
        provider_owner = context.provider_owner
        persist = context.persist
        state = context.dispatch.state
        persisted_assistant_message_id = persist.persisted_assistant_message_id
        persisted_assistant_content = persist.persisted_assistant_content
        persisted_tool_call_turn = persist.persisted_tool_call_turn
        failed_turn = persist.failed_turn
        last_runtime_preflight = context.dispatch.last_runtime_preflight
        new_composition_turns_used = composition_turns_used + 1
        checkpoint_caller = None
        if context.checkpoint_phase_owner is not None:
            from elspeth.web.composer.continuation_checkpoint_end_locals import IterationEndCallerLocals

            if context.checkpoint_pending is None or context.checkpoint_pending.branch != "composition_last_chance":
                raise AuditIntegrityError("last-chance END checkpoint lost its original iteration continuation")
            checkpoint_caller = IterationEndCallerLocals(context.checkpoint_pending)
        try:
            if retained is None:
                runtime_result = await self._preflight.turn_runtime_preflight(
                    state=state,
                    user_id=user_id,
                    session_id=session_id,
                    last_runtime_preflight=last_runtime_preflight,
                    runtime_preflight_cache=runtime_preflight_cache,
                    initial_version=initial_version,
                    session_scope=session_scope,
                    recorder=recorder,
                    plugin_snapshot=plugin_snapshot,
                    session_operation_context=session_operation_context,
                )
            else:
                from elspeth.web.composer.continuation_classifier_handoff import decode_classifier_runtime_preflight

                runtime_result = decode_classifier_runtime_preflight(retained.runtime_preflight_json)
            advisor_gate = await self._completion._evaluate_terminal_no_tool_advisor_gate(
                state=state,
                turn_evidence=turn_evidence,
                session_operation_context=session_operation_context,
                session_id=session_id,
                current_state_id=persist.current_state_id,
                assistant_message=assistant_message,
                llm_messages=None,
                recorder=recorder,
                progress=progress,
                advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
                repair_turns_used=repair_turns_used,
                persisted_assistant_message_id=persisted_assistant_message_id,
                persisted_assistant_content=persisted_assistant_content,
                persisted_tool_call_turn=persisted_tool_call_turn,
                allow_repair_continue=False,
                user_message=message,
                runtime_preflight=runtime_result,
                user_id=user_id,
                runtime_preflight_cache=runtime_preflight_cache,
                initial_version=initial_version,
                session_scope=session_scope,
                plugin_snapshot=plugin_snapshot,
                advisor_review_state=advisor_review_state or _AdvisorReviewState(),
                deadline=deadline,
                completion_gates=completion_gates,
                provider_owner=provider_owner,
                checkpoint_phase_owner=context.checkpoint_phase_owner,
                checkpoint_caller=checkpoint_caller,
            )
        except _AdvisorCheckpointComposeDeadlineExpired:
            # The model had already replied; the timeout envelope
            # carries no prose, so keep the finished reply.
            await self._completion._persist_withheld_reply(
                "compose_deadline_expired",
                assistant_message.content or "",
                session_id=session_id,
                session_operation_context=session_operation_context,
            )
            raise ComposerConvergenceError.capture(
                max_turns=new_composition_turns_used + discovery_turns_used,
                budget_exhausted="timeout",
                state=state,
                initial_version=initial_version,
                tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                llm_calls=recorder.llm_calls,
                failed_turn=failed_turn,
            ) from None
        return await self._continue_last_chance_classifier_after_advisor_gate(
            context=context, assistant_message=assistant_message, advisor_gate=advisor_gate
        )

    async def _continue_last_chance_classifier_after_advisor_gate(
        self,
        *,
        context: _ClassifierCompletionContext,
        assistant_message: _AdmittedAssistantMessage,
        advisor_gate: _TerminalNoToolAdvisorGateOutcome,
    ) -> _ClassifyOutcome:
        """Resume the retained last-chance caller at its actual post-END cut."""
        session_operation_context = context.session_operation_context
        state = context.dispatch.state
        session_id, persist = context.session_id, context.persist
        progress, recorder = context.progress, context.recorder
        initial_version, user_id = context.initial_version, context.user_id
        last_runtime_preflight = context.dispatch.last_runtime_preflight
        runtime_preflight_cache = context.runtime_preflight_cache
        session_scope, message = context.session_scope, context.message
        mutation_success_seen = context.mutation_success_seen
        repair_turns_used = context.repair_turns_used
        plugin_snapshot, deadline = context.plugin_snapshot, context.deadline
        completion_gates = context.completion_gates
        persisted_assistant_message_id = persist.persisted_assistant_message_id
        persisted_assistant_content = persist.persisted_assistant_content
        persisted_tool_call_turn = persist.persisted_tool_call_turn
        if advisor_gate.action == "return":
            advisor_result = advisor_gate.result
            classified = _ClassifyOutcome(
                action="return",
                result=advisor_result,
                composition_turns_delta=1,
                advisor_passes_delta=advisor_gate.advisor_passes_delta,
                publication_binding=advisor_gate.publication_binding,
            )
            if context.completion_capture is not None:
                terminal = _TerminateOutcome(
                    action="return",
                    result=advisor_result,
                    advisor_passes_delta=advisor_gate.advisor_passes_delta,
                    advisor_review_state=advisor_gate.advisor_review_state,
                    publication_binding=advisor_gate.publication_binding,
                )
                context.completion_capture.record_original_model_end(terminal, classified)
            return classified
        # B-4D-3 budget-exhaustion last-chance finalize is a SECOND
        # no-tool finalize path. Route it through the SHARED
        # ``_surface_and_finalize_no_tools`` (Task 7 HIGH-1) so the
        # backend PT auto-surface AND the fail-closed orphan gate are
        # UNIVERSAL — a prior mutation can leave a required PT review
        # orphaned, and this reply can no longer surface it through
        # tools. The loop persists any mutation BEFORE
        # classify (dispatch -> persist -> ``current_state_id =
        # persist.current_state_id`` -> classify), so ``state`` matches
        # ``persist.current_state_id`` and the create_pending gate
        # holds. Thread the already-consumed repair budget through
        # this alternate terminal return just as P2 does.
        result = await self._completion._surface_and_finalize_no_tools(
            session_operation_context=session_operation_context,
            assistant_message=assistant_message,
            state=state,
            session_id=session_id,
            current_state_id=persist.current_state_id,
            progress=progress,
            recorder=recorder,
            initial_version=initial_version,
            user_id=user_id,
            last_runtime_preflight=last_runtime_preflight,
            runtime_preflight_cache=runtime_preflight_cache,
            session_scope=session_scope,
            message=message,
            mutation_success_seen=mutation_success_seen,
            repair_turns_used=repair_turns_used,
            plugin_snapshot=plugin_snapshot,
            deadline=deadline,
        )
        result = _with_advisor_gate_decision(
            result, completion_gates, advisor_gate.advisor_gate_decision, advisor_gate.graph_review_identity
        )
        threaded = replace(
            result,
            repair_turns_used=repair_turns_used,
            persisted_assistant_message_id=persisted_assistant_message_id,
            persisted_assistant_content=persisted_assistant_content,
            persisted_tool_call_turn=persisted_tool_call_turn,
        )
        classified = _ClassifyOutcome(
            action="return",
            result=threaded,
            composition_turns_delta=1,
            advisor_passes_delta=advisor_gate.advisor_passes_delta,
            publication_binding=advisor_gate.publication_binding,
        )
        if context.completion_capture is not None:
            terminal = _TerminateOutcome(
                action="return",
                result=threaded,
                advisor_passes_delta=advisor_gate.advisor_passes_delta,
                advisor_review_state=advisor_gate.advisor_review_state,
                publication_binding=advisor_gate.publication_binding,
                end_disposition=_end_disposition_after_terminal_barriers(threaded, advisor_gate),
            )
            context.completion_capture.record_original_model_end(terminal, classified)
        return classified

    async def advance_continuation_phase(
        self,
        *,
        owner: ContinuationLivePhases,
        authority: ComposerAsyncOperationAuthority,
        selected: SelectedContinuationState,
        work: RequiredWorkBinding,
        provider_owner: ProviderInvocationOwner,
        phase_read: _CurrentPhaseRead | None = None,
    ) -> ToolNoExecutionRefusal | None:
        """Enter the retained phase owner without re-entering legacy PRIMARY."""
        from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

        if (
            type(owner) is not ContinuationLivePhases
            or owner.service is not self
            or owner.authority is not authority
            or owner.work is not work
            or owner.provider_owner is not provider_owner
        ):
            raise AuditIntegrityError("continuation service dispatch changed its actual live owners")
        if phase_read is None:
            return await owner.advance(selected)
        else:
            return await owner.advance(selected, phase_read=phase_read)

    def _new_primary_custody(self, provider_owner: ProviderInvocationOwner | None) -> ProviderCallCustody | None:
        if provider_owner is None:
            return None
        if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():
            raise AuditIntegrityError("Primary invocation owner belongs to another exact service")
        return provider_owner.mint(ProviderInvocationFamily.PRIMARY)

    async def _call_llm_before_deadline(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        state: CompositionState,
        initial_version: int,
        deadline: float,
        recorder: BufferingRecorder | None = None,
        *,
        composition_turns_used: int,
        discovery_turns_used: int,
        failed_turn: FailedTurnMetadata | None,
        provider_owner: ProviderInvocationOwner | None = None,
        prepared_input: PreparedProviderInput | None = None,
        classifier_primary_issuer: ClassifierPrimaryIssuer | None = None,
    ) -> _AdmittedLLMCompletion | VerifiedClassifierPaidOverflowTerminal | ClassifierNoSdkRefusalReceipt:
        """Preserve the completion-returning deadline boundary."""
        outcome = await self._call_llm_outcome_before_deadline(
            messages,
            tools,
            state,
            initial_version,
            deadline,
            recorder=recorder,
            composition_turns_used=composition_turns_used,
            discovery_turns_used=discovery_turns_used,
            failed_turn=failed_turn,
            provider_owner=provider_owner,
            prepared_input=prepared_input,
            classifier_primary_issuer=classifier_primary_issuer,
        )
        from elspeth.web.sessions.continuation_classifier_primary import VerifiedClassifierPaidOverflowTerminal
        from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt

        if isinstance(outcome, (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt)):
            if type(outcome) not in (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt):
                raise AuditIntegrityError("classifier PRIMARY returned a non-nominal terminal")
            return outcome
        return outcome.completion

    async def _call_llm_outcome_before_deadline(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        state: CompositionState,
        initial_version: int,
        deadline: float,
        recorder: BufferingRecorder | None = None,
        *,
        composition_turns_used: int,
        discovery_turns_used: int,
        failed_turn: FailedTurnMetadata | None,
        provider_owner: ProviderInvocationOwner | None = None,
        prepared_input: PreparedProviderInput | None = None,
        classifier_primary_issuer: ClassifierPrimaryIssuer | None = None,
    ) -> _CallModelOutcome | VerifiedClassifierPaidOverflowTerminal | ClassifierNoSdkRefusalReceipt:
        """Call the LLM with a per-call timeout derived from the deadline.

        The Gateway retains admitted provider accounting and joins its owned
        SQL work when the deadline cancels a provider coroutine. If the deadline
        has already passed or the call exceeds the remaining budget, raise
        ComposerConvergenceError with the current partial state.

        ``recorder`` is the in-flight :class:`BufferingRecorder` from
        :meth:`_compose_loop` (or ``None`` from test paths). When set,
        timeout-based ``ComposerConvergenceError`` raises include the
        buffer's ``tool_invocations`` so the route handler's audit
        persistence has the per-call decision trail even when the
        budget exhaustion was a wall-clock timeout (no LLM mutation
        in this final call).

        The turn counters and ``failed_turn`` are owned by the caller, so
        both are required keyword arguments rather than defaulted ones
        (R2-F9, elspeth-114dd261bc). The two wall-clock raises below used to
        hardcode ``max_turns=0`` and omit ``failed_turn``, which told the
        user the composer gave up "within 0 turns" after a multi-turn build
        and — because the SPA's RecoveryPanel gates on ``failed_turn !=
        null`` — hid the salvaged partial pipeline the route handler had
        already persisted. Defaulting them would let a future call site
        silently reintroduce exactly that.

        Unlike the two budget raises in :meth:`_classify_and_charge_turn`,
        the count reported here is the plain sum of turns already spent: a
        wall-clock timeout does not trip (and so does not charge) either
        turn budget.
        """

        def _captured_invocations() -> tuple[ComposerToolInvocation, ...]:
            return recorder.invocations if recorder is not None else ()

        def _captured_llm_calls() -> tuple[ComposerLLMCall, ...]:
            return recorder.llm_calls if recorder is not None else ()

        provider_custody = self._new_primary_custody(provider_owner) if classifier_primary_issuer is None else None
        attempt = 0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ComposerConvergenceError.capture(
                    max_turns=composition_turns_used + discovery_turns_used,
                    budget_exhausted="timeout",
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=_captured_invocations(),
                    llm_calls=_captured_llm_calls(),
                    failed_turn=failed_turn,
                )
            try:
                if classifier_primary_issuer is not None:
                    if type(prepared_input) is not PreparedProviderInput or type(recorder) is not BufferingRecorder:
                        raise AuditIntegrityError("classifier PRIMARY lost its original prepared request or recorder")
                    issued = await classifier_primary_issuer.invoke(
                        branch="composition_last_chance",
                        prepared_input=prepared_input,
                        recorder=recorder,
                        deadline=deadline,
                        credential_state=state,
                    )
                    from elspeth.web.sessions.continuation_classifier_primary import VerifiedClassifierPaidOverflowTerminal
                    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt

                    if isinstance(issued, (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt)):
                        if type(issued) not in (VerifiedClassifierPaidOverflowTerminal, ClassifierNoSdkRefusalReceipt):
                            raise AuditIntegrityError("classifier PRIMARY returned a non-nominal terminal")
                        return issued
                    return issued.outcome
                if attempt and prepared_input is not None:
                    prepared_input = self._provider_gateway.reprepare_input(prepared_input)
                    messages = prepared_input.evidence.messages
                    tools = prepared_input.tools if prepared_input.tools is not None else []
                if provider_custody is None:
                    completion = await self._provider_gateway._call_llm_with_audit(
                        messages,
                        tools,
                        timeout=remaining,
                        recorder=recorder,
                        credential_state=state,
                        prepared_input=prepared_input,
                    )
                    return _CallModelOutcome(completion=completion)
                return await self._provider_gateway._call_llm_outcome_with_audit(
                    messages,
                    tools,
                    timeout=remaining,
                    recorder=recorder,
                    provider_custody=provider_custody,
                    credential_state=state,
                    prepared_input=prepared_input,
                )
            except (PromptGuardRefused, ComposerContextUnsupported) as exc:
                raise exc.with_evidence(
                    state=state,
                    initial_version=state.version,
                    llm_calls=_captured_llm_calls(),
                    failed_turn=failed_turn,
                ) from exc
            except TimeoutError:
                raise ComposerConvergenceError.capture(
                    max_turns=composition_turns_used + discovery_turns_used,
                    budget_exhausted="timeout",
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=_captured_invocations(),
                    llm_calls=_captured_llm_calls(),
                    failed_turn=failed_turn,
                ) from None
            except _BadRequestLLMError:
                # Bad-request from provider: never retry. 400s are not transient,
                # and the carrier holds the provider's status code + detail on
                # dedicated attributes for the outer handler to build the HTTP
                # detail. The redacted str(exc) intentionally does NOT leak
                # provider text; only ``expose_provider_error=True`` surfaces
                # ``provider_detail``/``provider_status_code``.
                #
                # Reciprocal contract: the route layer reads those two
                # attributes via ``_litellm_error_detail`` in
                # ``web/sessions/routes/_helpers.py`` (and the parallel call
                # site in ``web/execution/routes.py:evaluate_run_diagnostics``).
                # Any future bad-request carrier that subclasses this or
                # supersedes it MUST populate both ``provider_detail`` and
                # ``provider_status_code`` for the HTTP surface to remain
                # useful — otherwise the route falls back to the redacted
                # class-name wrap and operators lose triage data.
                raise
            except ChargeableAdmissionRefused as exc:
                # A previous failed physical attempt has already been settled
                # before the retry admission decision. Preserve its buffered
                # audit sidecar without inventing a new call for the refusal.
                attach_llm_calls(exc, recorder)
                raise
            except (ComposerAdmissionRefused, AuditIntegrityError, TypeError, ValueError, RecursionError) as exc:
                # Retry preparation is outside the gateway audit body. Its
                # credential/native refusal still retains earlier attempts.
                attach_llm_calls(exc, recorder)
                raise
            except OpenAIError as exc:
                if classifier_primary_issuer is not None:
                    # Its one actual attempt has durable custody; original
                    # failure propagation must precede the legacy retry loop.
                    raise
                failure = classify_provider_failure(exc)
                if failure is not None and failure.kind == "timeout":
                    raise ComposerConvergenceError.capture(
                        max_turns=composition_turns_used + discovery_turns_used,
                        budget_exhausted="timeout",
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=_captured_invocations(),
                        llm_calls=_captured_llm_calls(),
                        failed_turn=failed_turn,
                    ) from None
                if failure is None or not failure.retryable:
                    raise
                attempt += 1
                if attempt >= LLM_API_MAX_ATTEMPTS:
                    raise
                delay_seconds = LLM_API_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                remaining_after_error = deadline - asyncio.get_event_loop().time()
                if remaining_after_error <= delay_seconds:
                    raise
                await asyncio.sleep(delay_seconds)


# ---------------------------------------------------------------------------
# Test-only compose-loop driver result carrier.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ComposeLoopDiagnostics:
    """Facts owned by one compose call while P3 and P4 finish."""

    tool_outcomes: tuple[_ToolOutcome, ...] = ()
    pre_state_id: str | None = None
    redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...] = ()
    redacted_tool_rows: tuple[RedactedToolRow, ...] = ()
    audit_outcome: AuditOutcome | None = None


# Deterministic advisor checkpoint primitives. The verdict is module-level
# because both service methods and focused unit tests consume it.
