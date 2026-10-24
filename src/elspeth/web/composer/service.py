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
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
    from elspeth.web.composer.redaction_telemetry import RedactionTelemetry
    from elspeth.web.composer.turn_effects import BlobDeliveryAttempt, TurnEvidence
    from elspeth.web.sessions.protocol import (
        SessionServiceProtocol,
    )
    from elspeth.web.sessions.telemetry import _SessionsTelemetry

import structlog
from openai import OpenAIError
from opentelemetry import metrics
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.blobs import BlobServiceProtocol
from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateToolAuthoring
from elspeth.contracts.composer_llm_audit import (
    ComposerLLMCall,
)
from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
    detach_prompt_guard_evidence,
    detach_prompt_guard_messages,
)
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.compartments import ChatIngressInput, CompositionIngressRecord, chat_ingress_input, compartment_ingress_record
from elspeth.web.composer import no_tool_policy as _no_tool_policy
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedToolCall,
    _AdvisorReviewState,
    _CallModelOutcome,
    _ClassifyOutcome,
    _ComposeIterationBindings,
    _ComposeIterationState,
    _DispatchOutcome,
    _PersistOutcome,
    _ToolBatchCancellationRequested,
    _ToolOutcome,
)
from elspeth.web.composer.advisor_checkpoint import (
    AdvisorCheckpointOwner,
    _AdvisorCheckpointComposeDeadlineExpired,
)
from elspeth.web.composer.advisor_decision import (
    AdvisorGateDecision,
)
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.audit import BufferingRecorder, ComposerToolCancellation, ComposerToolFailure
from elspeth.web.composer.availability import ComposerAvailability as ComposerAvailability  # re-export; genuine home is availability.py
from elspeth.web.composer.availability import compute_availability
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.composition_completion import (
    CompositionCompletion,
    _append_interpretation_review_handoff_message,
    _with_advisor_gate_decision,
)
from elspeth.web.composer.continuation_message_builders import (
    build_service_initial_messages,
    recovery_instruction_message,
    reply_instruction_message,
)
from elspeth.web.composer.control_messages import anti_anchor_control_envelope
from elspeth.web.composer.discovery_cache import (
    CachedDiscoveryPayload as _CachedDiscoveryPayload,
)
from elspeth.web.composer.discovery_cache import (
    RuntimePreflightCache as _RuntimePreflightCache,
)
from elspeth.web.composer.discovery_response import admit_discovery_result, preview_source_proof_stop
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.interpretation_surfacing import InterpretationSurfacing
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.llm_response_parsing import (
    attach_llm_calls,
)
from elspeth.web.composer.progress import (
    convergence_progress_event,
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
    ComposerConvergenceError,
    ComposerHistoryMessage,
    ComposerMutationAuthorityRevoked,
    ComposerPluginCrashError,
    ComposerResult,
    ComposerRuntimePreflightError,
    ComposerServiceError,
    ComposerSettings,
    GraphRecoveryResponse,
    RetainedOperationContext,
    retained_operation_context_message,
)
from elspeth.web.composer.provider_config import (
    LLM_API_MAX_ATTEMPTS,
    LLM_API_RETRY_BASE_DELAY_SECONDS,
    infer_provider_from_model_name,
    infer_provider_from_unprefixed_model_name,
)
from elspeth.web.composer.provider_cost import CommonProviderCostRefused, capture_common_provider_cost
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
    recovery_context_message,
    recovery_tool_definitions,
)
from elspeth.web.composer.request_history import authoritative_conversation_context
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.composer.session_tool import SessionToolOwner
from elspeth.web.composer.skills import assert_skill_hash_unchanged_on_disk
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.strict_transport import (
    ComposerToolContractSummary,
    StrictTransportDiagnostic,
    resolve_composer_tool_contract,
)
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, ProofUnavailable
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.credential_guard import require_no_credential_material, require_no_credential_material_in_state
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


async def _await_tool_turn_with_deferred_cancellation[T](
    awaitable: Awaitable[T],
    *,
    cancellation_requested: asyncio.Event,
) -> tuple[T, asyncio.CancelledError | None]:
    task = asyncio.ensure_future(awaitable)
    deferred: list[asyncio.CancelledError] = []

    def retained_cancellation(child_error: BaseException | None = None) -> asyncio.CancelledError:
        first = deferred[0]
        originals: list[BaseException] = []
        prior = first.__cause__
        for error in (*deferred[1:], prior, child_error):
            if error is not None and error is not first and all(error is not item for item in originals):
                originals.append(error)
        if originals:
            first.__cause__ = BaseExceptionGroup("Tool turn retained original outcomes", originals)
        return first

    def raise_joined_child(child_error: BaseException) -> NoReturn:
        cancellation = retained_cancellation(child_error)
        if type(child_error) is ComposerToolFailure:
            # Keep the established evidence carrier, and make every original
            # reachable through standard links for outer classifiers.
            carrier = ComposerToolCancellation(
                cancellation,
                tool_invocations=child_error.tool_invocations,
                llm_calls=child_error.llm_calls,
            )
            try:
                raise carrier from child_error.original_exception
            except ComposerToolCancellation:
                carrier.__context__ = cancellation
                raise
        raise cancellation

    while True:
        try:
            result = await asyncio.shield(task)
        except _ToolBatchCancellationRequested:
            if not deferred:
                raise
            cancelled = retained_cancellation()
            raise cancelled from cancelled.__cause__
        except asyncio.CancelledError as original:
            if task.cancelled():
                # Read the actual child outcome, retaining its evidence carrier.
                try:
                    task.result()
                except BaseException as child_error:
                    if not deferred:
                        raise
                    raise_joined_child(child_error)
                raise AssertionError("A cancelled child returned a result") from original
            if all(original is not item for item in deferred):
                deferred.append(original)
            cancellation_requested.set()
            if not task.done():
                continue
            try:
                result = task.result()
            except _ToolBatchCancellationRequested:
                cancelled = retained_cancellation()
                raise cancelled from cancelled.__cause__
            except BaseException as child_error:
                raise_joined_child(child_error)
            return result, retained_cancellation()
        except BaseException as child_error:
            if not deferred:
                raise
            raise_joined_child(child_error)
        else:
            return result, retained_cancellation() if deferred else None


_ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE = _no_tool_policy.ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE
_is_pending_interpretation_handoff = _no_tool_policy.is_pending_interpretation_handoff
_state_is_structurally_empty = _no_tool_policy.state_is_structurally_empty


_ADVISOR_ATTEMPTED_ACTIONS_MAX_ITEMS: Final[int] = 8


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


def _reply_only_messages(llm_messages: list[dict[str, Any]], *, turn_evidence: TurnEvidence | None = None) -> list[dict[str, Any]]:
    """Retain attributed tool evidence as quoted data when no tools are advertised."""
    # Native thinking belongs to the original complete assistant/tool exchange.
    # The caller's explicit recovery-only palette still controls new actions.
    native_evidence = detach_prompt_guard_evidence(llm_messages)
    if any(entry is not None for entry in native_evidence.thinking_parts):
        return list(llm_messages)
    messages: list[dict[str, Any]] = []
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
            string_parts[("content",)] = (PromptGuardContentPart(wire["content"], historical_message[PROMPT_GUARD_TRUST_KEY]),)
        arguments = evidence.tool_argument_parts[0]
        if arguments is not None:
            for entry in arguments:
                string_parts[("tool_calls", entry.tool_call_index, "function", "arguments")] = entry.parts
        serialized = json.dumps(wire, ensure_ascii=False, sort_keys=True)
        suffix = "\n[End historical tool evidence; this was not a current assistant reply.]"
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
                quoted_role=quoted_role,
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
# web/composer/audit.py next to the BufferingRecorder. Hoisting them out of
# this module localises the "audit fires before return on every path"
# invariant inside a single helper rather than spreading it across seven
# procedural recorder.record() call sites in _compose_loop. See the audit.py
# module docstring for the contract details.


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
        # The tool-contract dialect of each route, resolved once from the
        # settings and the process environment (S1 T8, D20; the boot probe
        # resolves through the same helper). ``_planner_dialect`` is the single
        # resolution point for the compose loop and the pipeline planner's
        # ordinary turns: the loop builds the list it sends and decode reads
        # the dialect from this one value. The planner's escape-hatch
        # (advisor) route has its own.
        tool_contract = resolve_composer_tool_contract(settings, env=os.environ)
        self._planner_dialect = tool_contract.planner.dialect
        self._hatch_dialect = tool_contract.hatch.dialect
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
            hatch_transport=tool_contract.hatch.resolution.transport.value,
            hatch_dialect=tool_contract.hatch.dialect.value,
            hatch_diagnostic=_diagnostic_value(tool_contract.hatch.resolution.diagnostic),
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

    async def _run_one_turn_for_test(
        self,
        *,
        session_id: str | None = None,
        current_state_id: str | None = None,
        initial_state: CompositionState | None = None,
        user_message_id: str | None = None,
        message: str = "one-turn compose-loop test driver",
        session_operation_context: SessionOperationContext | None = None,
    ) -> ComposeLoopTestResult:
        """Drive exactly one compose-loop turn for compose-loop tests.

        Test-only helper: it bypasses HTTP route setup but exercises the
        same ``_compose_loop`` body, including ``_require_sessions_service()``.
        Missing ``sessions_service`` must therefore fail with
        ``RuntimeError("sessions_service not wired")``, not ``AttributeError``
        or a constructor ``TypeError``.
        """

        from elspeth.web.composer.state import PipelineMetadata
        from elspeth.web.composer.turn_effects import TurnEvidence

        del user_message_id
        self._require_sessions_service()
        state = initial_state or CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        resolved_session_id = session_id or "00000000-0000-0000-0000-000000000000"
        plugin_snapshot, policy_catalog = self._policy_context.build(None)
        diagnostics = _ComposeLoopDiagnostics()
        test_preferences = await self._require_sessions_service().get_composer_preferences(UUID(resolved_session_id))
        test_ceiling = ComposerMutationCeiling(resolved_session_id, str(uuid4()), test_preferences.trust_mode)
        test_evidence = TurnEvidence(
            session_id=resolved_session_id,
            session_operation_context=session_operation_context,
            request_id=test_ceiling.request_id,
            request=message,
            initial_state_id=current_state_id,
            initial_state=state,
        )
        result = await self._compose_loop(
            message,
            [],
            state,
            session_id=resolved_session_id,
            initial_current_state_id=current_state_id,
            deadline=asyncio.get_event_loop().time() + self._timeout_seconds,
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            mutation_ceiling=test_ceiling,
            session_operation_context=session_operation_context,
            diagnostics=diagnostics,
            turn_evidence=test_evidence,
        )

        return ComposeLoopTestResult(
            assistant_message=result.message,
            raw_assistant_content=result.raw_assistant_content,
            persisted_assistant_content=result.persisted_assistant_content,
            persisted_assistant_matches_terminal_model_turn=result.persisted_assistant_matches_terminal_model_turn,
            tool_outcomes=diagnostics.tool_outcomes,
            persisted_assistant_tool_calls=diagnostics.redacted_assistant_tool_calls,
            persisted_tool_row_content=tuple(row.content for row in diagnostics.redacted_tool_rows),
            audit_outcome=diagnostics.audit_outcome,
            pre_state_id=diagnostics.pre_state_id,
            tool_invocations=result.tool_invocations,
            runtime_preflight=result.runtime_preflight,
            advisor_gate_decision=result.advisor_gate_decision,
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

    @staticmethod
    def _require_required_work(required_work: RequiredWorkBinding | None, context: SessionOperationContext | None) -> RequiredWorkBinding:
        if type(required_work) is not RequiredWorkBinding:
            raise AuditIntegrityError("Required composer work has no explicit registered owner")
        required_work.validate_context(context)
        return required_work

    async def compose_recovery(
        self,
        *,
        message: str,
        recovery_context: GraphRecoveryContext,
        session_operation_context: SessionOperationContext,
        required_work: RequiredWorkBinding,
        provider_owner: ProviderInvocationOwner,
        budget_seconds: float | None = None,
    ) -> GraphRecoveryResponse:
        """Ask the normal provider for a terminal action without hydrating a corrupt head."""
        if not self._availability.available:
            raise ComposerServiceError(self._availability.reason or "Composer is unavailable.")
        if type(provider_owner) is not ProviderInvocationOwner:
            raise AuditIntegrityError("Recovery provider owner must be an owned invocation authority")
        if provider_owner.service is not self._require_sessions_service() or provider_owner.required_work is not required_work:
            raise AuditIntegrityError("Recovery provider owner replaced the actual turn binding")
        if type(session_operation_context) is not SessionOperationContext:
            raise AuditIntegrityError("Recovery requires an owned session authority")
        required_work.validate_context(session_operation_context)
        if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
            raise AuditIntegrityError("Recovery requires COMPOSE session authority")
        require_no_credential_material(message, surface="composer_recovery_request")
        await self._chargeable_admission.require(session_operation_context)
        remaining = self._timeout_seconds if budget_seconds is None else budget_seconds
        if remaining <= 0:
            raise ComposerServiceError("Recovery provider deadline expired; no graph was restored.")
        recorder = BufferingRecorder()
        with composer_quota_scope(self._require_sessions_service(), session_operation_context), prompt_guard_user_scope(message):
            completion = await self._provider_gateway._call_llm_with_audit(
                [
                    recovery_instruction_message(),
                    {"role": "user", "content": recovery_context_message(recovery_context)},
                    {"role": "user", "content": message},
                ],
                recovery_tool_definitions(self._planner_dialect),
                timeout=remaining,
                recorder=recorder,
                provider_custody=self._new_primary_custody(provider_owner),
            )
        try:
            intent = parse_recovery_tool_batch(completion.tool_batch)
            if intent is None:
                raise ComposerServiceError("The model did not select a recovery action; no graph was restored.")
        except (ValueError, ComposerServiceError) as exc:
            attach_llm_calls(exc, recorder)
            raise
        return GraphRecoveryResponse(intent=intent, llm_calls=recorder.llm_calls)

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

    async def compose(
        self,
        message: str,
        messages: list[ComposerHistoryMessage],
        state: CompositionState,
        session_id: str | None = None,
        current_state_id: str | None = None,
        user_id: str | None = None,
        progress: ComposerProgressSink | None = None,
        user_message_id: str | None = None,
        session_operation_context: SessionOperationContext | None = None,
        # Durable advisor gate fact from the prior state row (ruling
        # 2026-09-22). ``None`` = none known: the END gate reviews as before.
        completion_gates: CompletionGateFacts | None = None,
        budget_seconds: float | None = None,
        required_work: RequiredWorkBinding | None = None,
        provider_owner: ProviderInvocationOwner | None = None,
        recovery_context: GraphRecoveryContext | None = None,
        retained_operation_context: RetainedOperationContext | None = None,
        advisor_history: AdvisorTranscriptHistory | None = None,
        *,
        mutation_ceiling: ComposerMutationCeiling | None = None,
    ) -> ComposerResult:
        """Run the LLM composition loop with dual-counter budget.

        Args:
            message: The user's chat message.
            messages: Chat history pre-converted from ChatMessageRecord by the
                route handler (seam contract B), including its internal marker
                on exact persisted human-user rows.
            state: The current CompositionState.
            current_state_id: Database id of ``state`` when it came from a
                persisted session row. Used as the stale-state guard for
                compose-loop tool-call audit persistence.

        Returns:
            ComposerResult with assistant message and updated state.

        Raises:
            ComposerConvergenceError: If a budget is exhausted or
                the timeout is exceeded.
        """
        if retained_operation_context is not None:
            if type(retained_operation_context) is not RetainedOperationContext:
                raise TypeError("Retained operation context must be an owned value")
            retained_operation_context.validate_for(session_id=session_id, actor_user_id=user_id)
        if (required_work is None) != (provider_owner is None):
            raise AuditIntegrityError("Required Composer provider ownership must be supplied with its turn binding")
        if provider_owner is not None:
            if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():
                raise AuditIntegrityError("Composer provider owner belongs to another exact service")
            if provider_owner.required_work is not required_work:
                raise AuditIntegrityError("Composer provider owner replaced its actual turn binding")
            provider_owner.required_work.validate_context(session_operation_context)

        if not self._availability.available:
            raise ComposerServiceError(self._availability.reason or "Composer is unavailable.")
        if session_operation_context is not None:
            if type(session_operation_context) is not SessionOperationContext:
                raise TypeError("session_operation_context must be an exact SessionOperationContext")
            if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
                raise ValueError("composer custody requires COMPOSE session authority")
            if session_id is None or session_operation_context.fence.session_id != session_id:
                raise AuditIntegrityError("Composer session authority targets a different session")

        env_ref_names = (
            frozenset(item.name for item in self._secret_service.list_refs(user_id))
            if self._secret_service is not None and user_id is not None
            else frozenset()
        )
        require_no_credential_material_in_state(
            state,
            surface="composer_state_before_provider",
            env_ref_names=env_ref_names,
        )

        if mutation_ceiling is not None and user_message_id is None:
            raise AuditIntegrityError("Supplied mutation ceiling lacks an original user message identity")
        logical_request_id = user_message_id or str(uuid4())
        if session_id is not None and mutation_ceiling is None:
            initial_preferences = await self._require_sessions_service().get_composer_preferences(UUID(session_id))
            mutation_ceiling = ComposerMutationCeiling(session_id, logical_request_id, initial_preferences.trust_mode)
        if mutation_ceiling is not None:
            if (
                type(mutation_ceiling) is not ComposerMutationCeiling
                or mutation_ceiling.session_id != session_id
                or mutation_ceiling.request_id != logical_request_id
            ):
                raise AuditIntegrityError("Compose received another logical request's mutation ceiling")
            if session_operation_context is not None:
                mutation_ceiling.validate_context(session_operation_context)

        await self._chargeable_admission.require(session_operation_context)
        with composer_quota_scope(self._require_sessions_service(), session_operation_context), prompt_guard_user_scope(message):
            turn_budget = self._timeout_seconds if budget_seconds is None else budget_seconds
            if turn_budget <= 0:
                raise ComposerConvergenceError.capture(
                    0,
                    budget_exhausted="timeout",
                    state=state,
                    initial_version=state.version,
                )
            deadline = asyncio.get_event_loop().time() + turn_budget
            # One recorder spans planning or the ordinary loop so every provider
            # and discovery audit for this request is accounted for.
            recorder = BufferingRecorder()
            from elspeth.web.composer.turn_effects import TurnEvidence

            turn_evidence = TurnEvidence(
                session_id=session_id,
                session_operation_context=session_operation_context,
                request_id=logical_request_id,
                request=message,
                initial_state_id=current_state_id,
                initial_state=state,
                retained_operation_context=retained_operation_context,
                actor_user_id=user_id,
                advisor_history=advisor_history,
            )
            if self._session_engine is not None and session_id is not None and session_operation_context is not None:
                from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader

                turn_evidence.transcript.use_blob_reader(
                    CurrentTranscriptBlobReader.from_service(
                        self,
                        session_id=session_id,
                        context=session_operation_context,
                    )
                )
            plugin_snapshot, policy_catalog = self._policy_context.build(user_id)
            try:
                if provider_owner is not None and session_id is not None and user_message_id is not None:
                    cost_budget = provider_owner.cost_budget
                    if cost_budget is None:
                        cost_budget = capture_common_provider_cost(
                            session_id=session_id,
                            request_id=user_message_id,
                            lifecycle_version=1,
                            initially_empty=_state_is_structurally_empty(state),
                            settings=self._settings,
                        )
                        provider_owner.bind_cost_budget(cost_budget)
                    cost_budget.validate_binding(
                        session_id=session_id,
                        request_id=user_message_id,
                        lifecycle_version=1,
                    )
                    cost_budget.require_supported_configuration()
                return await self._compose_loop(
                    message,
                    messages,
                    state,
                    session_id,
                    current_state_id,
                    user_id,
                    deadline,
                    progress,
                    user_message_id,
                    recorder=recorder,
                    plugin_snapshot=plugin_snapshot,
                    policy_catalog=policy_catalog,
                    session_operation_context=session_operation_context,
                    completion_gates=completion_gates,
                    required_work=required_work,
                    provider_owner=provider_owner,
                    recovery_context=recovery_context,
                    retained_operation_context=retained_operation_context,
                    mutation_ceiling=mutation_ceiling,
                    turn_evidence=turn_evidence,
                )
            except CommonProviderCostRefused as exc:
                attach_llm_calls(exc, recorder)
                raise
            except AuditIntegrityError as exc:
                attach_llm_calls(exc, recorder)
                raise
            except BaseExceptionGroup as exc:
                attach_llm_calls(exc, recorder)
                raise
            except ComposerConvergenceError as exc:
                await emit_progress(
                    progress,
                    convergence_progress_event(budget_exhausted=exc.budget_exhausted),
                )
                # Has its own partial_state; route handler persists. Do not intercept.
                raise
            except ComposerPluginCrashError as crash:
                # Plugin-bug crash path. The exception already carries
                # partial_state (populated by _compose_loop at the execute_tool
                # site when state.version > initial_version), so the route
                # handler can persist the accumulated mutations into
                # composition_states symmetrically with the convergence path.
                #
                # Here we only add the session-row audit breadcrumb (updated_at
                # bump — richer crash-marker columns tracked as a follow-up
                # migration: elspeth-23b0987938). A compose with no session
                # (trained-operator and MCP paths) has no row to mark, so the
                # skip is explicit (elspeth-906bc8f75d).
                if session_id is not None:
                    if session_operation_context is None or self._sessions_service is None:
                        # A session-bound compose reached the crash path without
                        # its COMPOSE lease. The breadcrumb is a sessions-table
                        # write and every such write goes through the fenced
                        # authority; writing it on a raw engine here would be the
                        # one unfenced sessions writer in the tree. Refusing is
                        # the fail-closed shape: this is a caller defect, so it
                        # deliberately escapes the audit-failure catch below with
                        # the plugin crash chained as its cause.
                        raise RuntimeError("plugin crash breadcrumb requires the COMPOSE session operation context") from crash
                    authority = self._sessions_service.session_operation_authority
                    try:
                        # Offload to a worker — the fenced mutation executes a
                        # synchronous transaction + UPDATE, which would otherwise
                        # block the event loop for the duration of the DB
                        # round-trip, stalling websocket heartbeats, rate-limit
                        # checks, and concurrent progress broadcasts. Symmetric
                        # with the execute_tool offload at the top of
                        # _compose_loop: every other sync DB path in this file
                        # runs through run_sync_in_worker.
                        await run_sync_in_worker(
                            authority.mutate,
                            session_operation_context,
                            lambda transaction: transaction.session.record_plugin_crash_breadcrumb(),
                        )
                    except (SQLAlchemyError, OSError, SessionOperationFenceLost) as audit_failure:
                        # Audit-persistence is best-effort on the crash path —
                        # failure to persist MUST NOT mask the original plugin
                        # bug. Log via slog.error (audit system itself is failing
                        # here, which is one of the permitted slog use cases).
                        #
                        # Catch is narrowed to (SQLAlchemyError, OSError) so that
                        # programmer-bug exceptions propagate instead of being
                        # laundered as "audit failure".
                        #
                        # exc_info is deliberately omitted: exception messages
                        # may carry DB URLs, filesystem paths, or secret fragments.
                        slog.error(
                            "composer_crash_persistence_failed",
                            session_id=session_id,
                            original_exc_class=crash.exc_class,
                            audit_exc_class=type(audit_failure).__name__,
                        )
                await emit_progress(
                    progress,
                    ComposerProgressEvent(
                        phase="failed",
                        headline="The composer could not safely finish this request.",
                        evidence=("A pipeline tool failed on the server side.",),
                        likely_next="Review the visible error message, then retry after the issue is resolved.",
                        reason="plugin_crash",
                    ),
                )
                raise
            except PromptGuardRefused:
                # Each invocation owner captured its exact undrained evidence.
                # A completed planner drain must remain empty here.
                raise
            except ComposerMutationAuthorityRevoked:
                raise
            except ComposerServiceError:
                # Generic service-level failure (prompt prep, availability check,
                # or a LiteLLMAPIError surfacing through the inner loop). The
                # route handlers further narrow provider failures; here the
                # service emits the safe catch-all.
                await emit_progress(
                    progress,
                    ComposerProgressEvent(
                        phase="failed",
                        headline="The composer could not finish this request.",
                        evidence=("The model call or prompt preparation failed safely.",),
                        likely_next="Retry once the composer service is available.",
                        reason="service_setup_failed",
                    ),
                )
                raise

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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], BlobDeliveryAttempt | None]:
        """Freeze the selected input and derive the gateway's exact marked hash."""
        from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers, supports_anthropic_prompt_cache_markers

        selected_messages, selected_tools = deepcopy(messages), deepcopy(tools)
        delivery = None
        if turn_evidence is not None:
            turn_evidence.transcript.observe_messages(messages)
            marked_messages, marked_tools = selected_messages, selected_tools
            if supports_anthropic_prompt_cache_markers(self._model):
                marked_messages, marked_tools_or_none = apply_anthropic_cache_markers(
                    selected_messages, selected_tools, mark_history_tail=True
                )
                marked_tools = marked_tools_or_none if marked_tools_or_none is not None else selected_tools
            delivery = turn_evidence.prepare_delivery(
                detach_prompt_guard_messages(marked_messages)[0],
                previous_calls=recorder.llm_calls,
                tools=marked_tools or None,
                scope="primary",
            )
        return selected_messages, selected_tools, delivery

    async def _call_model_turn(
        self,
        *,
        llm_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        state: CompositionState,
        initial_version: int,
        deadline: float,
        recorder: BufferingRecorder,
        progress: ComposerProgressSink | None,
        message: str,
        composition_turns_used: int,
        discovery_turns_used: int,
        failed_turn: FailedTurnMetadata | None,
        provider_owner: ProviderInvocationOwner | None = None,
        turn_evidence: TurnEvidence | None = None,
    ) -> _CallModelOutcome:
        """Phase P1 of the compose loop — one LLM call with cap enforcement.

        Emits the model-call progress event, calls the provider before the
        cooperative deadline, and enforces ``_max_tool_calls_per_turn``.
        A cap breach raises :class:`ComposerConvergenceError` with the
        ``tool_call_cap_exceeded`` reason directly; no carrier is returned
        in that case.

        ``failed_turn`` is the driver's running metadata for the LAST
        persisted tool-call turn — carried here only so a wall-clock timeout
        on this call can report it (R2-F9). It is ``None`` on the first loop
        iteration and whenever the loop runs without a session, because no
        turn has been persisted yet.
        """
        await emit_progress(progress, model_call_progress_event(message))
        selected_messages, selected_tools, delivery = self._prepare_primary_delivery(
            messages=llm_messages, tools=tools, recorder=recorder, turn_evidence=turn_evidence
        )
        if provider_owner is None:
            # Existing completion hooks remain the warm/legacy boundary.
            completion = await self._call_llm_before_deadline(
                selected_messages,
                selected_tools,
                state,
                initial_version,
                deadline,
                recorder=recorder,
                composition_turns_used=composition_turns_used,
                discovery_turns_used=discovery_turns_used,
                failed_turn=failed_turn,
                provider_owner=provider_owner,
            )
            outcome = _CallModelOutcome(completion=completion)
        else:
            outcome = await self._call_llm_outcome_before_deadline(
                selected_messages,
                selected_tools,
                state,
                initial_version,
                deadline,
                recorder=recorder,
                composition_turns_used=composition_turns_used,
                discovery_turns_used=discovery_turns_used,
                failed_turn=failed_turn,
                provider_owner=provider_owner,
            )
            completion = outcome.completion
        assistant_tool_calls = completion.tool_batch.calls
        self._enforce_tool_call_cap(
            assistant_tool_calls=assistant_tool_calls,
            state=state,
            initial_version=initial_version,
            composition_turns_used=composition_turns_used,
            discovery_turns_used=discovery_turns_used,
            recorder=recorder,
            failed_turn=failed_turn,
            persisted_tool_call_turn=False,
        )

        if turn_evidence is not None and delivery is not None:
            turn_evidence.confirm_delivery(delivery, recorder.llm_calls)
        return outcome

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
        )

    async def _dispatch_tool_batch(
        self,
        *,
        call_model: _CallModelOutcome,
        mutation_ceiling: ComposerMutationCeiling | None,
        state: CompositionState,
        last_validation: ValidationSummary | None,
        last_runtime_preflight: ValidationResult | None,
        llm_messages: list[dict[str, Any]],
        recorder: BufferingRecorder,
        anti_anchor: AntiAnchorTracker,
        discovery_cache: dict[str, _CachedDiscoveryPayload],
        runtime_preflight_cache: _RuntimePreflightCache,
        session_id: str | None,
        session_operation_context: SessionOperationContext | None = None,
        user_id: str | None,
        user_message_id: str | None,
        user_message_content: str | None,
        current_state_id: str | None,
        actor: str,
        initial_version: int,
        deadline: float,
        progress: ComposerProgressSink | None,
        session_scope: str,
        advisor_calls_used: int,
        cancellation_requested: asyncio.Event,
        plugin_snapshot: PluginAvailabilitySnapshot,
        policy_catalog: PolicyCatalogView,
        composition_turns_used: int,
        discovery_turns_used: int,
        failed_turn: FailedTurnMetadata | None,
        provider_owner: ProviderInvocationOwner | None = None,
        turn_evidence: TurnEvidence | None = None,
        message_producers: CurrentContinuationMessageProducers | None = None,
    ) -> tuple[_DispatchOutcome, int]:
        """Phase P3 of the compose loop — delegates to :func:`tool_batch.run_tool_batch`."""
        from elspeth.web.composer.tool_batch import (
            BatchAccumulator,
            ToolBatchContext,
            ToolBatchCustody,
            ToolBatchProvenance,
            run_tool_batch,
        )

        turn_sessions_service = self._require_sessions_service() if session_id is not None else None
        turn_session_uuid = UUID(session_id) if session_id is not None else None
        turn_preferences = (
            await turn_sessions_service.get_composer_preferences(turn_session_uuid)
            if turn_sessions_service is not None and turn_session_uuid is not None
            else None
        )
        review_required = False
        if turn_session_uuid is not None:
            if type(mutation_ceiling) is not ComposerMutationCeiling or turn_preferences is None:
                raise AuditIntegrityError("Session mutation dispatch lost its request ceiling")
            if mutation_ceiling.session_id != str(turn_session_uuid):
                raise AuditIntegrityError("Mutation dispatch crossed sessions")
            if session_operation_context is not None:
                mutation_ceiling.validate_context(session_operation_context)
            if turn_evidence is not None and mutation_ceiling.request_id != turn_evidence.request_id:
                raise AuditIntegrityError("Mutation dispatch crossed logical requests")
            review_required = not mutation_ceiling.permits_auto(turn_preferences.trust_mode)
        elif mutation_ceiling is not None:
            raise AuditIntegrityError("Sessionless mutation dispatch received a mutation ceiling")
        ctx = ToolBatchContext(
            session_tools=self._session_tools,
            provenance=ToolBatchProvenance(
                model_identifier=self._model,
                provider=self._availability.provider,
                skill_hash=self._composer_skill_hash,
            ),
            custody=ToolBatchCustody(
                data_dir=self._data_dir,
                session_engine=self._session_engine,
                secret_service=self._secret_service,
                secret_wiring_policy=self._secret_wiring_policy,
                max_blob_storage_per_session_bytes=self._settings.max_blob_storage_per_session_bytes,
            ),
            redaction_telemetry=self._redaction_telemetry,
            preflight=self._preflight,
            schema_disclosure=self._schema_disclosure,
            advisor_checkpoint=self._advisor_checkpoint,
            provider_owner=provider_owner,
            advisor_max_calls_per_compose=self._settings.composer_advisor_max_calls_per_compose,
            advisor_timeout_seconds=self._settings.composer_advisor_timeout_seconds,
            recorder=recorder,
            anti_anchor=anti_anchor,
            discovery_cache=discovery_cache,
            runtime_preflight_cache=runtime_preflight_cache,
            session_id=session_id,
            session_operation_context=session_operation_context,
            user_id=user_id,
            user_message_id=user_message_id,
            user_message_content=user_message_content,
            current_state_id=current_state_id,
            actor=actor,
            initial_version=initial_version,
            deadline=deadline,
            progress=progress,
            session_scope=session_scope,
            turn_sessions_service=turn_sessions_service,
            turn_session_uuid=turn_session_uuid,
            turn_preferences=turn_preferences,
            mutation_ceiling=mutation_ceiling,
            review_required=review_required,
            cancellation_requested=cancellation_requested,
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            tool_contract_dialect=self._planner_dialect,
            session_operation_authority=(turn_sessions_service.session_operation_authority if turn_sessions_service is not None else None),
            composition_turns_used=composition_turns_used,
            discovery_turns_used=discovery_turns_used,
            failed_turn=failed_turn,
            turn_evidence=turn_evidence,
            message_producers=message_producers,
        )
        acc = BatchAccumulator(
            state=state,
            last_validation=last_validation,
            last_runtime_preflight=last_runtime_preflight,
            advisor_calls_used=advisor_calls_used,
        )
        return await run_tool_batch(
            call_model=call_model,
            ctx=ctx,
            acc=acc,
            llm_messages=llm_messages,
        )

    async def _classify_and_budget_turn(
        self,
        *,
        dispatch: _DispatchOutcome,
        turn_evidence: TurnEvidence,
        persist: _PersistOutcome,
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
    ) -> _ClassifyOutcome:
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
        persisted_assistant_message_id = persist.persisted_assistant_message_id
        persisted_assistant_content = persist.persisted_assistant_content
        failed_turn = persist.failed_turn

        # P4 and original failure/cancellation primacy have completed. Reuse
        # the current preview's observation without resolving the blob again:
        # newly ready bytes cannot reopen this request's primary dialogue.
        source_stop: ProofUnavailable | MalformedSourceProofClaim | None = None
        if dispatch.tool_outcomes:
            terminal = dispatch.tool_outcomes[-1]
            if (
                terminal.call.function.name == "preview_pipeline"
                and terminal.error_class is None
                and isinstance(terminal.response, ToolResult)
            ):
                source_stop = preview_source_proof_stop(admit_discovery_result("preview_pipeline", terminal.response), state)
        terminal_without_preview = _tool_batch_staged_terminal_interpretation_review_handoff(dispatch.tool_outcomes) or (
            turn_has_mutation and composition_turns_used + 1 >= self._max_composition_turns
        )
        if source_stop is None and terminal_without_preview:
            assessment = self._completion._assess_source_proof(
                state, session_id=session_id, session_operation_context=session_operation_context
            )
            if type(assessment) in (ProofUnavailable, MalformedSourceProofClaim):
                source_stop = cast(ProofUnavailable | MalformedSourceProofClaim, assessment)
        if source_stop is not None:
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
            )
            return _ClassifyOutcome(
                action="return",
                result=stopped.result,
                composition_turns_delta=int(turn_has_mutation),
                discovery_turns_delta=int(turn_has_discovery and not turn_has_mutation),
                advisor_passes_delta=stopped.advisor_passes_delta,
                publication_binding=stopped.publication_binding,
            )

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
                remaining = deadline - asyncio.get_event_loop().time()
                # A reply with no advertised tools cannot carry protocol tool
                # blocks on providers such as Bedrock Converse. Preserve each
                # complete historical record as attributed text instead; the
                # planning/audit history stays unchanged and no tool is enabled.
                reply_messages = _reply_only_messages(llm_messages, turn_evidence=turn_evidence)
                reply_messages.append(reply_instruction_message())
                provider_failures: tuple[type[Exception], ...] = (TimeoutError, _BadRequestLLMError, *advisor_provider_failure_types())
                selected_reply_messages, selected_reply_tools, reply_delivery = self._prepare_primary_delivery(
                    messages=reply_messages,
                    tools=recovery_tool_definitions(self._planner_dialect),
                    recorder=recorder,
                    turn_evidence=turn_evidence,
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
                        completion = await self._provider_gateway._call_llm_with_audit(
                            selected_reply_messages,
                            selected_reply_tools,
                            timeout=remaining,
                            recorder=recorder,
                            provider_custody=self._new_primary_custody(provider_owner),
                        )
                    except provider_failures:
                        # The call audit retains the failure; a trusted notice
                        # below reports the unavailable reply to the user.
                        reply = None
                    else:
                        recovery_result = self._terminal_recovery_result(
                            completion, state, recorder, prior_tools_persisted=persisted_tool_call_turn
                        )
                        if recovery_result is not None:
                            return _ClassifyOutcome(action="return", result=recovery_result)
                        reply = completion.message
                        if _reply_only_protocol_echo(
                            reply.content or "",
                            history_has_tool_protocol=any(row["role"] == "tool" or "tool_calls" in row for row in llm_messages),
                            user_message=message,
                        ):
                            reply = None
                        if reply is not None and not completion.tool_batch.calls and reply_delivery is not None:
                            turn_evidence.confirm_delivery(reply_delivery, recorder.llm_calls)
                advisor_gate = None
                if reply is not None:
                    try:
                        advisor_gate = await self._completion._evaluate_terminal_no_tool_advisor_gate(
                            state=state,
                            turn_evidence=turn_evidence,
                            session_operation_context=session_operation_context,
                            session_id=session_id,
                            current_state_id=persist.current_state_id,
                            assistant_message=reply,
                            llm_messages=llm_messages,
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
                        raw_assistant_content=(
                            "" if handoff_result.raw_assistant_content is None else handoff_result.raw_assistant_content
                        ),
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
                return _ClassifyOutcome(
                    action="return",
                    result=threaded,
                    composition_turns_delta=1 if turn_has_mutation else 0,
                    discovery_turns_delta=1 if turn_has_discovery else 0,
                    advisor_passes_delta=advisor_gate.advisor_passes_delta if advisor_gate is not None else 0,
                    publication_binding=advisor_gate.publication_binding if advisor_gate is not None else None,
                )

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
                selected_final_messages, selected_final_tools, final_delivery = self._prepare_primary_delivery(
                    messages=llm_messages, tools=tools, recorder=recorder, turn_evidence=turn_evidence
                )
                completion = await self._call_llm_before_deadline(
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
                )
                recovery_result = self._terminal_recovery_result(
                    completion, state, recorder, prior_tools_persisted=persisted_tool_call_turn
                )
                if recovery_result is not None:
                    return _ClassifyOutcome(action="return", result=recovery_result)
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
                    try:
                        advisor_gate = await self._completion._evaluate_terminal_no_tool_advisor_gate(
                            state=state,
                            turn_evidence=turn_evidence,
                            session_operation_context=session_operation_context,
                            session_id=session_id,
                            current_state_id=persist.current_state_id,
                            assistant_message=assistant_message,
                            llm_messages=llm_messages,
                            recorder=recorder,
                            progress=progress,
                            advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
                            repair_turns_used=repair_turns_used,
                            persisted_assistant_message_id=persisted_assistant_message_id,
                            persisted_assistant_content=persisted_assistant_content,
                            persisted_tool_call_turn=persisted_tool_call_turn,
                            allow_repair_continue=False,
                            user_message=message,
                            runtime_preflight=await self._preflight.turn_runtime_preflight(
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
                            ),
                            user_id=user_id,
                            runtime_preflight_cache=runtime_preflight_cache,
                            initial_version=initial_version,
                            session_scope=session_scope,
                            plugin_snapshot=plugin_snapshot,
                            advisor_review_state=advisor_review_state or _AdvisorReviewState(),
                            deadline=deadline,
                            completion_gates=completion_gates,
                            provider_owner=provider_owner,
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
                    if advisor_gate.action == "return":
                        advisor_result = advisor_gate.result
                        return _ClassifyOutcome(
                            action="return",
                            result=advisor_result,
                            composition_turns_delta=1,
                            advisor_passes_delta=advisor_gate.advisor_passes_delta,
                        )
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
                    return _ClassifyOutcome(
                        action="return",
                        result=threaded,
                        composition_turns_delta=1,
                        publication_binding=advisor_gate.publication_binding,
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

    async def _compose_loop(
        self,
        message: str,
        messages: list[ComposerHistoryMessage],
        state: CompositionState,
        session_id: str | None = None,
        initial_current_state_id: str | None = None,
        user_id: str | None = None,
        deadline: float = 0.0,
        progress: ComposerProgressSink | None = None,
        user_message_id: str | None = None,
        recorder: BufferingRecorder | None = None,
        *,
        plugin_snapshot: PluginAvailabilitySnapshot,
        policy_catalog: PolicyCatalogView,
        mutation_ceiling: ComposerMutationCeiling | None,
        session_operation_context: SessionOperationContext | None = None,
        # Durable advisor gate fact from the prior state row (ruling
        # 2026-09-22). ``None`` = none known: the END gate reviews as before.
        completion_gates: CompletionGateFacts | None = None,
        diagnostics: _ComposeLoopDiagnostics | None = None,
        required_work: RequiredWorkBinding | None = None,
        provider_owner: ProviderInvocationOwner | None = None,
        recovery_context: GraphRecoveryContext | None = None,
        retained_operation_context: RetainedOperationContext | None = None,
        turn_evidence: TurnEvidence | None = None,
    ) -> ComposerResult:
        """Inner composition loop with dual-counter budget tracking.

        The loop body is decomposed into five phases (see the carrier
        module ``_compose_loop_carriers`` for the dataclasses that
        thread state between them):

        * P1 :meth:`_call_model_turn`        — one LLM call, cap check
        * P2 :meth:`_try_terminate_no_tools` — handle the no-tool-calls
          branch (repair injections or finalize-and-return)
        * P3 :meth:`_dispatch_tool_batch`    — execute every tool call,
          accumulate ``_ToolOutcome`` records, rebind ``state``
        * P4 :meth:`_persist_turn_audit`     — redact, persist the turn
          audit row, raise plugin-crash propagation if applicable
        * P5 :meth:`_classify_and_budget_turn` — anti-anchor hint,
          cache-hit short-circuit, dual-counter budget classify,
          B-4D-3 last-chance LLM call

        Uses cooperative timeout: the deadline is checked at safe
        checkpoints (before LLM calls, after tool batches) rather
        than using asyncio.wait_for() cancellation.  This ensures
        tool calls that have filesystem/DB side effects always run
        to completion with their state published — no split between
        committed side effects and the response.

        LLM calls are wrapped in per-call asyncio.wait_for(remaining)
        because they are pure network I/O with no side effects and
        can be safely cancelled.

        Args:
            recorder: Optional request-scoped audit recorder. ``None`` creates
                a fresh recorder for direct and test-only callers.
        """
        initial_version = state.version
        from elspeth.web.composer.turn_effects import TurnEvidence

        if session_id is not None:
            if type(mutation_ceiling) is not ComposerMutationCeiling or mutation_ceiling.session_id != session_id:
                raise AuditIntegrityError("Session compose loop lost its original mutation ceiling")
            if user_message_id is not None and mutation_ceiling.request_id != user_message_id:
                raise AuditIntegrityError("Compose loop mutation ceiling crossed requests")
            if session_operation_context is not None:
                mutation_ceiling.validate_context(session_operation_context)
        elif mutation_ceiling is not None:
            raise AuditIntegrityError("Sessionless compose loop received a mutation ceiling")
        if turn_evidence is None:
            turn_evidence = TurnEvidence(
                session_id=session_id,
                session_operation_context=session_operation_context,
                request_id=mutation_ceiling.request_id if mutation_ceiling is not None else user_message_id or str(uuid4()),
                request=message,
                initial_state_id=initial_current_state_id,
                initial_state=state,
                retained_operation_context=retained_operation_context,
                actor_user_id=user_id,
            )
        elif (
            type(turn_evidence) is not TurnEvidence
            or turn_evidence.session_id != session_id
            or turn_evidence.request_hash != stable_hash(message)
            or turn_evidence.operation_id
            != (session_operation_context.fence.operation_id if session_operation_context is not None else None)
            or (user_message_id is not None and turn_evidence.request_id != user_message_id)
        ):
            raise AuditIntegrityError("Compose loop evidence belongs to a different request")
        if mutation_ceiling is not None and turn_evidence.request_id != mutation_ceiling.request_id:
            raise AuditIntegrityError("Compose loop evidence differs from its mutation ceiling")
        ingress = compartment_ingress_record(message, own_compartment_id=self._settings.compartment_id) if session_id is not None else None
        chat_ingress_inputs = (
            _chat_ingress_inputs_for_compose(
                message,
                messages,
                user_message_id=user_message_id,
                own_compartment_id=self._settings.compartment_id,
            )
            if session_id is not None
            else None
        )
        # F-5c. On the first compose-loop entry of this service instance,
        # upsert the composer skill markdown into
        # ``skill_markdown_history`` so an auditor inspecting a future
        # interpretation_events row can join via ``composer_skill_hash``
        # to retrieve the exact text the LLM was prompted with. The
        # ``INSERT OR IGNORE`` semantics make repeated calls cheap; we
        # still gate behind a per-instance flag so steady-state compose()
        # calls don't churn the connection pool.
        await self._maybe_upsert_skill_markdown_history()
        llm_messages = self._build_messages(
            messages,
            state,
            message,
            session_id=session_id,
            user_id=user_id,
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            session_operation_context=session_operation_context,
        )
        turn_evidence.transcript.begin_messages(llm_messages)
        tools = composer_loop_tool_definitions(self._planner_dialect)
        if recovery_context is not None:
            llm_messages.append({"role": "system", "content": recovery_context_message(recovery_context)})
        if retained_operation_context is not None:
            retained_operation_context.validate_for(session_id=session_id, actor_user_id=user_id)
        llm_messages.append({"role": "system", "content": retained_operation_context_message(retained_operation_context)})
        # Per-call audit recorder. Surfaced on ComposerResult and on
        # the three partial-state-carrier exceptions so the route handler
        # always has the per-call decision trail — including failure paths.
        # A caller that already buffered fast-path invocations (see the
        # docstring's ``recorder`` arg) passes them in so they are not lost.
        if recorder is None:
            recorder = BufferingRecorder()
        # Stable actor string for every invocation in this compose() call.
        # Falls back to "anonymous" when user_id is None (CLI/test paths);
        # the real web composer always has user_id from auth dependency.
        actor = f"composer-web:user-{user_id}" if user_id is not None else "composer-web:anonymous"
        await emit_progress(
            progress,
            ComposerProgressEvent(
                phase="starting",
                headline="I'm reading your request and current pipeline.",
                evidence=(
                    "The current pipeline state is prepared for the composer.",
                    "The pipeline composer skill pack and deployment overlay are included.",
                ),
                likely_next="ELSPETH will ask the model for the next safe pipeline action.",
            ),
        )

        composition_turns_used = 0
        discovery_turns_used = 0
        mutation_success_seen = False

        # Discovery cache: local variable scoped to this compose() call.
        # Keyed by (tool_name, canonical_args_json). Each concurrent
        # compose() call gets its own independent cache dict.
        discovery_cache: dict[str, _CachedDiscoveryPayload] = {}

        # Validation threading: compute once for the initial state, then
        # carry forward from each ToolResult.validation. Avoids redundant
        # validate() calls — CompositionState is immutable so validation
        # is deterministic for a given state object.
        last_validation: ValidationSummary | None = None

        # Runtime preflight cache: scoped to this compose() call. Keyed by
        # (session_scope, state_version, state_content_hash, settings_hash).
        # A timeout or failure is cached for the lifetime of this compose call
        # so subsequent preview_pipeline calls don't re-fire an already-failed
        # worker. Content identity prevents concurrent unsaved requests from
        # sharing a result solely because they both start at version zero.
        runtime_preflight_cache = self._preflight.new_cache()
        last_runtime_preflight: ValidationResult | None = None
        session_scope = f"session:{session_id}" if session_id is not None else "session:unsaved"

        # §7.7 anti-anchor tracker: detects 3-in-a-row identical failed tool
        # calls and injects a STRUCTURAL HINT before the next LLM turn so the
        # model breaks out of the anchored-loop pattern observed in the Tier 1
        # final cohort's residual RED. Per-compose-call instance — never
        # shared across requests.
        anti_anchor = AntiAnchorTracker()

        # Advisor escape-hatch budget. Local to this compose() call —
        # each fresh user request starts with the full configured budget.
        # Per-compose-request scope (matching the setting name
        # ``composer_advisor_max_calls_per_compose``) is the useful budget:
        # an LLM that breaks out of an anchored loop in one request should
        # not have its budget penalised in the next. There is intentionally
        # no session-lifetime cap; ``composer_rate_limit_per_minute`` and
        # the per-compose budget together bound advisor cost. When the
        # toggle is disabled the counter is never read.
        advisor_calls_used = 0

        # Forced-repair counter. When the assistant emits no tool_calls but
        # the proof step found blocking diagnostics, the loop synthesises a
        # repair message and continues for at most _MAX_REPAIR_TURNS
        # additional iterations. NEVER catches plugin exceptions — only
        # configuration diagnostics.
        repair_turns_used = 0
        # END-gate advisor pass counter (Task 6). Counts ONLY the END
        # authoritative checkpoint passes; the EARLY advisory pass (Task 5)
        # never touches it. Separate from ``repair_turns_used`` (D-8): a
        # turn is a correctness repair XOR an advisor repair, never both.
        advisor_checkpoint_passes_used = 0
        advisor_review_state = _AdvisorReviewState()
        persisted_assistant_message_id: str | None = None
        persisted_assistant_content: str | None = None
        persisted_tool_call_turn = False
        failed_turn: FailedTurnMetadata | None = None
        current_state_id: str | None = initial_current_state_id
        advisor_repair_context_introduced = False
        # Finalize-context elision (Task 6 Step 3, elspeth-bff8fe6864,
        # belt-and-braces on top of the output-contract clause in the
        # injected advisor message itself). Indices of FLAGGED advisor
        # sign-off messages still awaiting elision (a list, not a single
        # slot: consecutive FLAGGED-with-no-repair rounds can stack more
        # than one injection before a tool-call turn ever lands). Appended
        # to when a "continue" outcome carries ``advisor_injection_index``;
        # drained once the next tool-call turn's dispatch completes — at
        # that point the model has already acted on the advisor text (real
        # repair tool calls/results now carry the state change), so every
        # pending injected message is elided from ``llm_messages`` before
        # any further model call, including the eventual CLEAN finalize
        # turn. Never elided if the very next turn is ALSO no-tool-calls (an
        # immediate rebuttal with no repair attempt) — that path has no
        # dispatch checkpoint to hook and is covered by the output-contract
        # clause instead, not by elision.
        pending_advisor_elision_indices: list[int] = []

        iteration_state = _ComposeIterationState(
            state=state,
            initial_version=initial_version,
            llm_messages=llm_messages,
            tools=tools,
            recorder=recorder,
            composition_turns_used=composition_turns_used,
            discovery_turns_used=discovery_turns_used,
            mutation_success_seen=mutation_success_seen,
            discovery_cache=discovery_cache,
            last_validation=last_validation,
            runtime_preflight_cache=runtime_preflight_cache,
            last_runtime_preflight=last_runtime_preflight,
            anti_anchor=anti_anchor,
            advisor_calls_used=advisor_calls_used,
            repair_turns_used=repair_turns_used,
            advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
            advisor_review_state=advisor_review_state,
            persisted_assistant_message_id=persisted_assistant_message_id,
            persisted_assistant_content=persisted_assistant_content,
            persisted_tool_call_turn=persisted_tool_call_turn,
            failed_turn=failed_turn,
            current_state_id=current_state_id,
            advisor_repair_context_introduced=advisor_repair_context_introduced,
            pending_advisor_elision_indices=pending_advisor_elision_indices,
            turn_evidence=turn_evidence,
        )
        iteration_bindings = _ComposeIterationBindings(
            message=message,
            session_id=session_id,
            user_id=user_id,
            user_message_id=user_message_id,
            deadline=deadline,
            progress=progress,
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            mutation_ceiling=mutation_ceiling,
            session_operation_context=session_operation_context,
            completion_gates=completion_gates,
            diagnostics=diagnostics,
            required_work=required_work,
            provider_owner=provider_owner,
            ingress=ingress,
            chat_ingress_inputs=chat_ingress_inputs,
            actor=actor,
            session_scope=session_scope,
        )
        while True:
            result = await self._advance_live_compose_iteration(iteration_state, iteration_bindings)
            if result is not None:
                return result

    async def _advance_live_compose_iteration(
        self,
        live: _ComposeIterationState,
        bindings: _ComposeIterationBindings,
    ) -> ComposerResult | None:
        state = live.state
        initial_version = live.initial_version
        llm_messages = live.llm_messages
        tools = live.tools
        recorder = live.recorder
        composition_turns_used = live.composition_turns_used
        discovery_turns_used = live.discovery_turns_used
        mutation_success_seen = live.mutation_success_seen
        discovery_cache = live.discovery_cache
        last_validation = live.last_validation
        runtime_preflight_cache = live.runtime_preflight_cache
        last_runtime_preflight = live.last_runtime_preflight
        anti_anchor = live.anti_anchor
        advisor_calls_used = live.advisor_calls_used
        repair_turns_used = live.repair_turns_used
        advisor_checkpoint_passes_used = live.advisor_checkpoint_passes_used
        advisor_review_state = live.advisor_review_state
        persisted_assistant_message_id = live.persisted_assistant_message_id
        persisted_assistant_content = live.persisted_assistant_content
        persisted_tool_call_turn = live.persisted_tool_call_turn
        failed_turn = live.failed_turn
        current_state_id = live.current_state_id
        advisor_repair_context_introduced = live.advisor_repair_context_introduced
        pending_advisor_elision_indices = live.pending_advisor_elision_indices
        turn_evidence = live.turn_evidence
        message = bindings.message
        session_id = bindings.session_id
        user_id = bindings.user_id
        user_message_id = bindings.user_message_id
        deadline = bindings.deadline
        progress = bindings.progress
        plugin_snapshot = bindings.plugin_snapshot
        policy_catalog = bindings.policy_catalog
        mutation_ceiling = bindings.mutation_ceiling
        session_operation_context = bindings.session_operation_context
        completion_gates = bindings.completion_gates
        diagnostics = bindings.diagnostics
        required_work = bindings.required_work
        provider_owner = bindings.provider_owner
        ingress = bindings.ingress
        chat_ingress_inputs = bindings.chat_ingress_inputs
        actor = bindings.actor
        session_scope = bindings.session_scope
        for _continuation_iteration in (None,):
            try:
                # The compose-loop audit path captures the state id observed
                # before the provider call and passes this exact value to
                # persist_compose_turn_async as expected_current_state_id.
                call_model = await self._call_model_turn(
                    llm_messages=llm_messages,
                    tools=tools,
                    state=state,
                    initial_version=initial_version,
                    deadline=deadline,
                    recorder=recorder,
                    progress=progress,
                    message=message,
                    composition_turns_used=composition_turns_used,
                    discovery_turns_used=discovery_turns_used,
                    failed_turn=failed_turn,
                    provider_owner=provider_owner,
                    turn_evidence=turn_evidence,
                )
                recovery_result = self._terminal_recovery_result(
                    call_model.completion, state, recorder, prior_tools_persisted=persisted_tool_call_turn
                )
                if recovery_result is not None:
                    return recovery_result
                # If no tool calls, the LLM is done — apply the final gate and return
                if not call_model.completion.tool_batch.calls:
                    # A repair gate below may answer this reply with a user-role
                    # injection and continue. The reply has to be in the provider
                    # context first, or the next call shows a repair message
                    # answering an assistant turn the model cannot see. Appended
                    # BEFORE the gate so the gate's own
                    # ``advisor_injection_index = len(llm_messages)`` stays exact,
                    # and withdrawn again on ``action="return"``, where the reply
                    # is published rather than superseded and the provider context
                    # is left exactly as it was. A blank reply is skipped:
                    # providers reject empty assistant content, and there are no
                    # words to keep.
                    superseded_reply = call_model.completion.message.content or ""
                    superseded_reply_index: int | None = None
                    if superseded_reply.strip():
                        superseded_reply_index = len(llm_messages)
                        llm_messages.append({"role": "assistant", "content": superseded_reply})
                    terminate = await self._completion._try_terminate_no_tools(
                        assistant_message=call_model.completion.message,
                        turn_evidence=turn_evidence,
                        session_operation_context=session_operation_context,
                        message=message,
                        llm_messages=llm_messages,
                        state=state,
                        session_id=session_id,
                        current_state_id=current_state_id,
                        initial_version=initial_version,
                        user_id=user_id,
                        last_runtime_preflight=last_runtime_preflight,
                        runtime_preflight_cache=runtime_preflight_cache,
                        session_scope=session_scope,
                        mutation_success_seen=mutation_success_seen,
                        recorder=recorder,
                        progress=progress,
                        repair_turns_used=repair_turns_used,
                        persisted_assistant_message_id=persisted_assistant_message_id,
                        persisted_assistant_content=persisted_assistant_content,
                        persisted_tool_call_turn=persisted_tool_call_turn,
                        advisor_checkpoint_passes_used=advisor_checkpoint_passes_used,
                        plugin_snapshot=plugin_snapshot,
                        advisor_review_state=advisor_review_state,
                        deadline=deadline,
                        composition_turns_used=composition_turns_used,
                        discovery_turns_used=discovery_turns_used,
                        failed_turn=failed_turn,
                        completion_gates=completion_gates,
                        provider_owner=provider_owner,
                    )
                    if terminate.advisor_review_state is not None:
                        advisor_review_state = terminate.advisor_review_state
                    if terminate.action == "return":
                        if superseded_reply_index is not None:
                            del llm_messages[superseded_reply_index]
                        # Offensive guard (explicit raise, not assert): ``python -O``
                        # strips assert statements. The contract between
                        # ``_dispatch_terminate_phase`` and this caller is that
                        # ``result`` is non-None whenever ``action == "return"``;
                        # a None here would be a compose-loop bug, not a recoverable
                        # state. Routed to the HTTP-500 static-detail handler at
                        # ``routes/composer.py:905`` via :class:`InvariantError`
                        # (B1-sanitised response body).
                        if terminate.result is None:
                            raise InvariantError(
                                "_dispatch_terminate_phase returned action='return' with result=None — "
                                "the terminate-phase contract requires result to be set whenever the "
                                "phase signals a return."
                            )
                        public_result = terminate.result
                        if advisor_repair_context_introduced:
                            public_result = await self._completion._qualified_advisor_repair_public_result(
                                public_result,
                                user_id=user_id,
                                session_id=session_id,
                                session_operation_context=session_operation_context,
                                cache=runtime_preflight_cache,
                                initial_version=initial_version,
                                session_scope=session_scope,
                                plugin_snapshot=plugin_snapshot,
                                publication_binding=terminate.publication_binding,
                                turn_evidence=turn_evidence,
                                user_message=message,
                            )
                        return self._completion._append_checkpoint_receipt(public_result, turn_evidence=turn_evidence)
                    repair_turns_used += terminate.repair_turns_delta
                    advisor_checkpoint_passes_used += terminate.advisor_passes_delta
                    # The gate superseded this reply: it is never published, so keep
                    # the model's words as a non-rendered audit row.
                    if superseded_reply_index is not None:
                        await self._completion._persist_withheld_reply(
                            "repair_gate_superseded",
                            superseded_reply,
                            session_id=session_id,
                            session_operation_context=session_operation_context,
                        )
                    if terminate.advisor_injection_index is not None:
                        advisor_repair_context_introduced = True
                        if _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE:
                            pending_advisor_elision_indices.append(terminate.advisor_injection_index)
                            # Elide the superseded reply WITH the advisor message it
                            # was answered by. Dropping only the injection would
                            # leave this reply directly before the repair turn's
                            # assistant tool-call message — two consecutive
                            # assistant messages, which some providers reject.
                            if superseded_reply_index is not None:
                                pending_advisor_elision_indices.append(superseded_reply_index)
                    continue

                cancellation_requested = asyncio.Event()
                pre_batch_state = state

                async def _dispatch_and_persist_tool_turn(
                    _call_model: _CallModelOutcome = call_model,
                    _state: CompositionState = state,
                    _last_validation: ValidationSummary | None = last_validation,
                    _last_runtime_preflight: ValidationResult | None = last_runtime_preflight,
                    _current_state_id: str | None = current_state_id,
                    _advisor_calls_used: int = advisor_calls_used,
                    _cancellation_requested: asyncio.Event = cancellation_requested,
                    _persisted_tool_call_turn: bool = persisted_tool_call_turn,
                    _persisted_assistant_message_id: str | None = persisted_assistant_message_id,
                    _persisted_assistant_content: str | None = persisted_assistant_content,
                    _advisor_repair_context_introduced: bool = advisor_repair_context_introduced,
                    _composition_turns_used: int = composition_turns_used,
                    _discovery_turns_used: int = discovery_turns_used,
                    _failed_turn: FailedTurnMetadata | None = failed_turn,
                ) -> tuple[_DispatchOutcome, _PersistOutcome, int, bool, bool, BaseException | None]:
                    invocation_start = len(recorder.invocations)
                    try:
                        dispatch_result, updated_advisor_calls_used = await self._dispatch_tool_batch(
                            call_model=_call_model,
                            mutation_ceiling=mutation_ceiling,
                            session_operation_context=session_operation_context,
                            state=_state,
                            last_validation=_last_validation,
                            last_runtime_preflight=_last_runtime_preflight,
                            llm_messages=llm_messages,
                            recorder=recorder,
                            anti_anchor=anti_anchor,
                            discovery_cache=discovery_cache,
                            runtime_preflight_cache=runtime_preflight_cache,
                            session_id=session_id,
                            user_id=user_id,
                            user_message_id=user_message_id,
                            user_message_content=message,
                            current_state_id=_current_state_id,
                            actor=actor,
                            initial_version=initial_version,
                            deadline=deadline,
                            progress=progress,
                            session_scope=session_scope,
                            advisor_calls_used=_advisor_calls_used,
                            cancellation_requested=_cancellation_requested,
                            plugin_snapshot=plugin_snapshot,
                            policy_catalog=policy_catalog,
                            composition_turns_used=_composition_turns_used,
                            discovery_turns_used=_discovery_turns_used,
                            failed_turn=_failed_turn,
                            provider_owner=provider_owner,
                            turn_evidence=turn_evidence,
                        )
                    except ComposerRuntimePreflightError as preflight_failure:
                        raise ComposerRuntimePreflightError(
                            original_exc=preflight_failure.original_exc,
                            partial_state=preflight_failure.partial_state,
                            tool_invocations=recorder.invocations[invocation_start:],
                            llm_calls=preflight_failure.llm_calls,
                            failed_turn=preflight_failure.failed_turn,
                        ) from preflight_failure.original_exc
                    except (AssertionError, MemoryError, RecursionError, SystemError, AuditIntegrityError) as fatal_failure:
                        current_invocations = recorder.invocations[invocation_start:]
                        if not current_invocations:
                            raise
                        raise ComposerToolFailure(
                            fatal_failure,
                            tool_invocations=current_invocations,
                            llm_calls=recorder.llm_calls,
                        ) from fatal_failure
                    except asyncio.CancelledError as cancellation:
                        # P4 has not started. Earlier loop turns are already durable
                        # and must never be rebound by this batch's fallback drain.
                        raise ComposerToolCancellation(
                            cancellation,
                            tool_invocations=recorder.invocations[invocation_start:],
                            llm_calls=recorder.llm_calls,
                        ) from cancellation
                    if diagnostics is not None:
                        diagnostics.tool_outcomes = dispatch_result.tool_outcomes
                        diagnostics.pre_state_id = dispatch_result.pre_state_id

                    # Do not start a new advisory call after cancellation. If an
                    # advisory call was already running when cancellation landed,
                    # the enclosing shield lets it finish and P4 still publishes
                    # the completed audit prefix.
                    early_advisor_message_count = len(llm_messages)
                    early_checkpoint_deadline_expired = False
                    early_checkpoint_failure: BaseException | None = None
                    try:
                        if (
                            not _cancellation_requested.is_set()
                            and dispatch_result.advisor_compose_timeout is None
                            and dispatch_result.advisor_failure is None
                            and dispatch_result.authority_revocation is None
                        ):
                            try:
                                await self._advisor_checkpoint._maybe_run_early_checkpoint(
                                    state=dispatch_result.state,
                                    prev_state=_state,
                                    session_id=session_id,
                                    llm_messages=llm_messages,
                                    recorder=recorder,
                                    progress=progress,
                                    deadline=deadline,
                                    session_operation_context=session_operation_context,
                                    provider_owner=provider_owner,
                                    turn_evidence=turn_evidence,
                                )
                            except _AdvisorCheckpointComposeDeadlineExpired:
                                # The driver converts this signal after persistence
                                # and after plugin/cancellation primacy checks.
                                early_checkpoint_deadline_expired = True
                            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as checkpoint_failure:
                                # Defer the exact failure until P4's committed
                                # metadata has reached the driver. This is cleanup
                                # custody, not classification as a provider outage.
                                early_checkpoint_failure = checkpoint_failure
                    finally:
                        # The tool batch already ran. Its audit must be persisted
                        # even when an internal advisor failure unwinds this turn.
                        # A successful tool remains successful; the separate
                        # checkpoint failure propagates after this mandatory work.
                        early_advisor_context_introduced = len(llm_messages) > early_advisor_message_count
                        turn_evidence.transcript.observe_messages(llm_messages)
                        persist_result = await self._persist_turn_audit(
                            tool_outcomes=dispatch_result.tool_outcomes,
                            mutation_ceiling=mutation_ceiling,
                            authority_revocation=dispatch_result.authority_revocation,
                            base_state=_state,
                            decoded_args_by_call_id=dispatch_result.decoded_args_by_call_id,
                            private_authorings=dispatch_result.private_authorings,
                            private_invocations=dispatch_result.private_invocations,
                            assistant_message=dispatch_result.assistant_message,
                            raw_assistant_content=dispatch_result.raw_assistant_content,
                            assistant_tool_calls=dispatch_result.assistant_tool_calls,
                            crash_pending=(dispatch_result.plugin_crash is not None or dispatch_result.advisor_failure is not None),
                            session_id=session_id,
                            session_operation_context=session_operation_context,
                            current_state_id=_current_state_id,
                            persisted_tool_call_turn=_persisted_tool_call_turn,
                            persisted_assistant_message_id=_persisted_assistant_message_id,
                            persisted_assistant_content=_persisted_assistant_content,
                            ingress=ingress,
                            chat_ingress_inputs=chat_ingress_inputs,
                            advisor_repair_context_introduced=_advisor_repair_context_introduced,
                            required_work=required_work,
                            turn_evidence=turn_evidence,
                        )
                        if diagnostics is not None:
                            diagnostics.redacted_assistant_tool_calls = persist_result.redacted_assistant_tool_calls
                            diagnostics.redacted_tool_rows = persist_result.redacted_tool_rows
                            diagnostics.audit_outcome = persist_result.audit_outcome
                    return (
                        dispatch_result,
                        persist_result,
                        updated_advisor_calls_used,
                        early_advisor_context_introduced,
                        early_checkpoint_deadline_expired,
                        early_checkpoint_failure,
                    )

                (
                    (
                        dispatch,
                        persist,
                        advisor_calls_used,
                        early_advisor_context_introduced,
                        early_checkpoint_deadline_expired,
                        early_checkpoint_failure,
                    ),
                    deferred_cancel,
                ) = await _await_tool_turn_with_deferred_cancellation(
                    _dispatch_and_persist_tool_turn(),
                    cancellation_requested=cancellation_requested,
                )
                if early_advisor_context_introduced:
                    advisor_repair_context_introduced = True
                # State the driver still owns across iterations updates from
                # the dispatch carrier; persist + classify consume the rest
                # of the dispatch fields directly.
                state = dispatch.state if persist.authority_revocation is None else pre_batch_state
                if persist.authority_revocation is None:
                    last_validation = dispatch.last_validation
                    last_runtime_preflight = dispatch.last_runtime_preflight
                    if dispatch.mutation_success_observed:
                        mutation_success_seen = True
                    advisor_review_state = _record_advisor_repair_mutations(
                        advisor_review_state,
                        dispatch.tool_outcomes,
                    )
                current_state_id = persist.current_state_id
                persisted_assistant_message_id = persist.persisted_assistant_message_id
                persisted_assistant_content = persist.persisted_assistant_content
                persisted_tool_call_turn = persist.persisted_tool_call_turn
                failed_turn = persist.failed_turn
                pending_checkpoint_failures = tuple(
                    failure for failure in (early_checkpoint_failure, persist.effect_verification_failure) if failure is not None
                )
                # Finalize-context elision drain (Task 6 Step 3). Gated on
                # ``dispatch.mutation_success_observed`` — NOT merely "a tool-call
                # turn dispatched" — because a discovery-only turn (get_plugin_schema,
                # preview_pipeline, list_*) or an all-ARG_ERROR turn makes tool
                # calls without repairing anything. Draining on those would wipe
                # the advisor findings from context before any fix landed, so the
                # model's next no-tool reply "repairs" nothing, the END gate
                # re-flags on unchanged state, and the run needlessly blocks
                # (review finding 1). Only a turn that actually mutated state
                # counts as the repair the model was asked for.
                #
                # Residual (documented, not closed): a repair spanning TWO
                # mutating turns (e.g. a discovery turn to inspect the schema,
                # then the mutating fix on the turn after) still loses the
                # advisor text after the FIRST mutating turn, even though the
                # second mutating turn is still part of the same repair attempt.
                # Narrowed to the common single-mutating-turn case, not closed
                # for the general multi-turn repair case.
                if pending_advisor_elision_indices and dispatch.mutation_success_observed and persist.authority_revocation is None:
                    # Interleaved-turn boundary (review finding 3): a turn that
                    # emits BOTH prose and tool_calls keeps that prose verbatim in
                    # the appended assistant message (tool_batch.py) — elision
                    # only removes the injected advisor message itself, never the
                    # model's own reasoning/rebuttal prose from an interleaved
                    # turn. Deliberate: reasoning continuity for the model's own
                    # words outweighs a second-order anchoring risk that the
                    # Steps 1-2 output-contract clause already covers.
                    for elision_index in sorted(pending_advisor_elision_indices, reverse=True):
                        del llm_messages[elision_index]
                    pending_advisor_elision_indices = []
                if dispatch.plugin_crash is not None:
                    # Plugin-crash propagation discipline (plan §5.7): the
                    # capture in P3 already snapshotted `state` after every
                    # prior successful tool-call mutation. If persistence
                    # succeeded, re-capture with the post-persist failed_turn
                    # so the route layer's _handle_plugin_crash sees the
                    # complete partial-state story; otherwise raise the
                    # original capture as-is.
                    if persisted_tool_call_turn:
                        persisted_plugin_crash = ComposerPluginCrashError.capture(
                            dispatch.plugin_crash.original_exc,
                            state=state,
                            initial_version=initial_version,
                            tool_invocations=(),
                            llm_calls=recorder.llm_calls,
                            failed_turn=failed_turn,
                        )
                        if dispatch.plugin_crash_cause is None:
                            if pending_checkpoint_failures:
                                raise persisted_plugin_crash from BaseExceptionGroup(
                                    "Tool failure retained checkpoint failures", pending_checkpoint_failures
                                )
                            raise persisted_plugin_crash
                        if pending_checkpoint_failures:
                            raise persisted_plugin_crash from BaseExceptionGroup(
                                "Tool failure retained checkpoint failures",
                                (dispatch.plugin_crash_cause, *pending_checkpoint_failures),
                            )
                        raise persisted_plugin_crash from dispatch.plugin_crash_cause
                    if persist.unwind_audit_failed:
                        # P4 rolled back. Preserve only this unpersisted turn's
                        # suffix: recorder.invocations is request-cumulative, so
                        # carrying the full buffer after an earlier successful P4
                        # would duplicate already committed rows when the route
                        # drains the crash.
                        current_invocation_count = len(dispatch.tool_outcomes)
                        if current_invocation_count == 0 or current_invocation_count > len(dispatch.plugin_crash.tool_invocations):
                            raise InvariantError("plugin crash dispatch must carry one invocation per current-turn tool outcome")
                        unpersisted_plugin_crash = ComposerPluginCrashError.capture(
                            dispatch.plugin_crash.original_exc,
                            state=state,
                            initial_version=initial_version,
                            tool_invocations=dispatch.plugin_crash.tool_invocations[-current_invocation_count:],
                            llm_calls=recorder.llm_calls,
                            failed_turn=failed_turn,
                        )
                        if dispatch.plugin_crash_cause is None:
                            if pending_checkpoint_failures:
                                raise unpersisted_plugin_crash from BaseExceptionGroup(
                                    "Tool failure retained checkpoint failures", pending_checkpoint_failures
                                )
                            raise unpersisted_plugin_crash
                        if pending_checkpoint_failures:
                            raise unpersisted_plugin_crash from BaseExceptionGroup(
                                "Tool failure retained checkpoint failures",
                                (dispatch.plugin_crash_cause, *pending_checkpoint_failures),
                            )
                        raise unpersisted_plugin_crash from dispatch.plugin_crash_cause
                    # No session/audit target was configured, so nothing in the
                    # request-cumulative crash carrier has been persisted yet.
                    if dispatch.plugin_crash_cause is None:
                        if pending_checkpoint_failures:
                            raise dispatch.plugin_crash from BaseExceptionGroup(
                                "Tool failure retained checkpoint failures", pending_checkpoint_failures
                            )
                        raise dispatch.plugin_crash
                    if pending_checkpoint_failures:
                        raise dispatch.plugin_crash from BaseExceptionGroup(
                            "Tool failure retained checkpoint failures",
                            (dispatch.plugin_crash_cause, *pending_checkpoint_failures),
                        )
                    raise dispatch.plugin_crash from dispatch.plugin_crash_cause

                if dispatch.advisor_failure is not None:
                    # The advisor boundary deliberately preserves unclassified
                    # controlled-code faults instead of laundering them into a
                    # provider outage. P3 has already recorded PLUGIN_CRASH and
                    # P4 has published that tool row. A failed unwind publication
                    # is a new Tier-1 integrity fault and therefore takes primacy;
                    # otherwise re-raise the exact original exception object.
                    if persist.unwind_audit_failed:
                        raise AuditIntegrityError(
                            "Advisor failure audit row could not be persisted",
                            failed_turn=failed_turn,
                        ) from dispatch.advisor_failure
                    if isinstance(dispatch.advisor_failure, PromptGuardRefused):
                        advisor_refusal = dispatch.advisor_failure.with_evidence(
                            state=state,
                            initial_version=state.version,
                            tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                            llm_calls=recorder.llm_calls,
                            failed_turn=failed_turn,
                        )
                        if persist.effect_verification_failure is not None:
                            advisor_refusal.__cause__ = dispatch.advisor_failure
                            raise BaseExceptionGroup(
                                "Manual advisor guard refusal and committed effect verification failure",
                                (advisor_refusal, persist.effect_verification_failure),
                            ) from None
                        raise advisor_refusal from dispatch.advisor_failure
                    if persist.effect_verification_failure is not None:
                        prior_cause = dispatch.advisor_failure.__cause__
                        raise dispatch.advisor_failure from BaseExceptionGroup(
                            "Advisor failure and committed effect verification failure",
                            (persist.effect_verification_failure,)
                            if prior_cause is None
                            else (prior_cause, persist.effect_verification_failure),
                        )
                    raise dispatch.advisor_failure

                if deferred_cancel is not None:
                    # P3's in-flight tool and P4's atomic audit publication are
                    # now complete. Preserve any LLM-call audit carried by this
                    # compose request, then resume the original cancellation at
                    # the first safe checkpoint before P5 or another model turn.
                    attach_llm_calls(deferred_cancel, recorder)
                    if pending_checkpoint_failures:
                        prior_cause = deferred_cancel.__cause__
                        raise deferred_cancel from BaseExceptionGroup(
                            "Caller cancellation retained checkpoint failures",
                            pending_checkpoint_failures if prior_cause is None else (prior_cause, *pending_checkpoint_failures),
                        )
                    raise deferred_cancel

                if early_checkpoint_failure is not None:
                    if isinstance(early_checkpoint_failure, PromptGuardRefused):
                        early_refusal = early_checkpoint_failure.with_evidence(
                            state=state,
                            initial_version=state.version,
                            tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                            llm_calls=recorder.llm_calls,
                            failed_turn=failed_turn,
                        )
                        if persist.effect_verification_failure is not None:
                            # P4 already committed. Keep both originals visible to
                            # the operation reducer so a policy refusal cannot mask
                            # failure to verify the saved effect or replay its rows.
                            early_refusal.__cause__ = early_checkpoint_failure
                            raise BaseExceptionGroup(
                                "EARLY guard refusal and committed effect verification failure",
                                (early_refusal, persist.effect_verification_failure),
                            ) from None
                        raise early_refusal from early_checkpoint_failure
                    attach_llm_calls(early_checkpoint_failure, recorder)
                    if persist.effect_verification_failure is not None:
                        prior_cause = early_checkpoint_failure.__cause__
                        raise early_checkpoint_failure from BaseExceptionGroup(
                            "EARLY checkpoint failure retained committed effect verification failure",
                            (persist.effect_verification_failure,)
                            if prior_cause is None
                            else (prior_cause, persist.effect_verification_failure),
                        )
                    raise early_checkpoint_failure

                if dispatch.advisor_compose_timeout is not None:
                    current_invocation_count = len(dispatch.tool_outcomes)
                    if current_invocation_count == 0 or current_invocation_count > len(recorder.invocations):
                        raise InvariantError("advisor timeout dispatch must carry one invocation per current-turn tool outcome")
                    advisor_timeout = ComposerConvergenceError.capture(
                        max_turns=composition_turns_used + discovery_turns_used,
                        budget_exhausted="timeout",
                        state=state,
                        initial_version=initial_version,
                        # A successful P4 made the request's accumulated trail
                        # durable, so the route must not replay it. Without a
                        # persistence target, none of the prior turns is durable:
                        # preserve the recorder's complete in-memory trail.
                        tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=failed_turn,
                    )
                    if persist.effect_verification_failure is not None:
                        raise advisor_timeout from persist.effect_verification_failure
                    raise advisor_timeout

                if early_checkpoint_deadline_expired:
                    charged_composition_turns = composition_turns_used + (1 if dispatch.turn_has_mutation else 0)
                    charged_discovery_turns = discovery_turns_used + (
                        1 if dispatch.turn_has_discovery and not dispatch.turn_has_mutation else 0
                    )
                    early_timeout = ComposerConvergenceError.capture(
                        max_turns=charged_composition_turns + charged_discovery_turns,
                        budget_exhausted="timeout",
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=failed_turn,
                    )
                    if persist.effect_verification_failure is not None:
                        raise early_timeout from persist.effect_verification_failure
                    raise early_timeout

                if persist.effect_verification_failure is not None:
                    attach_llm_calls(persist.effect_verification_failure, recorder)
                    raise persist.effect_verification_failure

                if persist.authority_revocation is not None:
                    raise ComposerMutationAuthorityRevoked(
                        revocation=persist.authority_revocation,
                        llm_calls=recorder.llm_calls,
                        failed_turn=failed_turn,
                        state=state,
                        initial_version=initial_version,
                    )

                classify = await self._classify_and_budget_turn(
                    dispatch=dispatch,
                    turn_evidence=turn_evidence,
                    session_operation_context=session_operation_context,
                    persist=persist,
                    llm_messages=llm_messages,
                    tools=tools,
                    recorder=recorder,
                    anti_anchor=anti_anchor,
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
                    plugin_snapshot=plugin_snapshot,
                    advisor_review_state=advisor_review_state,
                    completion_gates=completion_gates,
                    provider_owner=provider_owner,
                )
                composition_turns_used += classify.composition_turns_delta
                discovery_turns_used += classify.discovery_turns_delta
                advisor_checkpoint_passes_used += classify.advisor_passes_delta
                repair_turns_used += classify.repair_turns_delta
                if classify.action == "return":
                    # Offensive guard (explicit raise, not assert): ``python -O``
                    # strips assert statements. The contract between
                    # ``_dispatch_classify_phase`` and this caller is that
                    # ``result`` is non-None whenever ``action == "return"``
                    # (the B-4D-3 last-chance branch sets it). A None here would
                    # be a compose-loop bug. Routed to the HTTP-500 static-detail
                    # handler at ``routes/composer.py:905`` via
                    # :class:`InvariantError` (B1-sanitised response body).
                    if classify.result is None:
                        raise InvariantError(
                            "_dispatch_classify_phase returned action='return' with result=None — "
                            "the classify-phase contract requires result to be set whenever the "
                            "phase signals a return."
                        )
                    if classify.result.graph_recovery_intent is not None:
                        return classify.result
                    public_result = classify.result
                    if advisor_repair_context_introduced:
                        public_result = await self._completion._qualified_advisor_repair_public_result(
                            public_result,
                            user_id=user_id,
                            session_id=session_id,
                            session_operation_context=session_operation_context,
                            cache=runtime_preflight_cache,
                            initial_version=initial_version,
                            session_scope=session_scope,
                            plugin_snapshot=plugin_snapshot,
                            publication_binding=classify.publication_binding,
                            turn_evidence=turn_evidence,
                            user_message=message,
                        )
                    return self._completion._append_checkpoint_receipt(public_result, turn_evidence=turn_evidence)
                continue
            finally:
                live.state = state
                live.initial_version = initial_version
                live.llm_messages = llm_messages
                live.tools = tools
                live.recorder = recorder
                live.composition_turns_used = composition_turns_used
                live.discovery_turns_used = discovery_turns_used
                live.mutation_success_seen = mutation_success_seen
                live.discovery_cache = discovery_cache
                live.last_validation = last_validation
                live.runtime_preflight_cache = runtime_preflight_cache
                live.last_runtime_preflight = last_runtime_preflight
                live.anti_anchor = anti_anchor
                live.advisor_calls_used = advisor_calls_used
                live.repair_turns_used = repair_turns_used
                live.advisor_checkpoint_passes_used = advisor_checkpoint_passes_used
                live.advisor_review_state = advisor_review_state
                live.persisted_assistant_message_id = persisted_assistant_message_id
                live.persisted_assistant_content = persisted_assistant_content
                live.persisted_tool_call_turn = persisted_tool_call_turn
                live.failed_turn = failed_turn
                live.current_state_id = current_state_id
                live.advisor_repair_context_introduced = advisor_repair_context_introduced
                live.pending_advisor_elision_indices = pending_advisor_elision_indices
                live.turn_evidence = turn_evidence
        return None

    def _build_messages(
        self,
        chat_history: list[ComposerHistoryMessage],
        state: CompositionState,
        user_message: str,
        session_id: str | None = None,
        user_id: str | None = None,
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        policy_catalog: PolicyCatalogView | None = None,
        *,
        session_operation_context: SessionOperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """Build the message list. Returns a NEW list on every call.

        This is critical: the tool-use loop appends to this list during
        iteration. Returning a cached reference would cause cross-turn
        contamination.

        OSError from deployment skill loading (PermissionError,
        IsADirectoryError) is translated into ComposerServiceError so
        the route handler returns a structured 502 rather than a raw 500.

        The HTTP body carries only ``type(exc).__name__`` — NOT
        ``str(exc)`` — because ``OSError.__str__`` expands to a string
        that includes the absolute filename (``[Errno 13] Permission
        denied: '/var/lib/elspeth/data/skills/...'``) which would
        leak filesystem layout and the operator's data-dir path into
        the 502 response body.  Full detail including the filename is
        preserved via ``raise ... from exc`` for the ASGI / server-log
        machinery only.  Mirrors the redaction contract landed by
        commits 1a30d985 (SQLAlchemy 422 path) and 127417cb (sibling
        HTTP-path slog sites) — both narrow the HTTP surface to
        class-name-only while preserving structured server-side detail.

        """
        if plugin_snapshot is None or policy_catalog is None:
            plugin_snapshot, policy_catalog = self._policy_context.build(user_id)
        return build_service_initial_messages(
            self,
            chat_history=chat_history,
            state=state,
            user_message=user_message,
            session_id=session_id,
            user_id=user_id,
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            session_operation_context=session_operation_context,
        )

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
    ) -> _AdmittedLLMCompletion:
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
        )
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
    ) -> _CallModelOutcome:
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

        provider_custody = self._new_primary_custody(provider_owner)
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
                if provider_custody is None:
                    completion = await self._provider_gateway._call_llm_with_audit(
                        messages, tools, timeout=remaining, recorder=recorder, credential_state=state
                    )
                    return _CallModelOutcome(completion=completion)
                return await self._provider_gateway._call_llm_outcome_with_audit(
                    messages,
                    tools,
                    timeout=remaining,
                    recorder=recorder,
                    provider_custody=provider_custody,
                    credential_state=state,
                )
            except PromptGuardRefused as exc:
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
            except OpenAIError as exc:
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


@dataclass(frozen=True, slots=True)
class ComposeLoopTestResult:
    """Structured result returned by the one-turn compose-loop test driver."""

    assistant_message: str
    raw_assistant_content: str | None = None
    tool_outcomes: tuple[Any, ...] = ()
    persisted_assistant_row: Any | None = None
    # What the compose loop already committed for the turn's assistant row,
    # threaded off ``ComposerResult.persisted_assistant_content``. Exposed so
    # compose-loop tests can pin the threading the turn-end writers depend on
    # to avoid re-emitting that row (elspeth-d581b3da7f) without reaching into
    # the route.
    persisted_assistant_content: str | None = None
    # Whether that row was persisted from the terminal model turn itself.
    persisted_assistant_matches_terminal_model_turn: bool = False
    persisted_assistant_tool_calls: tuple[Any, ...] = ()
    persisted_tool_row_content: tuple[Any, ...] = ()
    audit_outcome: AuditOutcome | None = None
    pre_state_id: str | None = None
    # Buffered per-call audit invocations so dispatch-branch tests can
    # assert recorder state without
    # touching the persistence machinery directly.
    tool_invocations: tuple[Any, ...] = ()
    # Final-gate ValidationResult carried on the returned ComposerResult.
    # Exposed so compose-loop tests can assert on the turn's readiness
    # (e.g. the fail-closed orphaned-interpretation gate) without bypassing
    # the production ``_compose_loop`` path.
    runtime_preflight: ValidationResult | None = None
    advisor_gate_decision: AdvisorGateDecision | None = None

    @property
    def tool_outcomes_for_assertion(self) -> tuple[Any, ...]:
        """Backward-compatible assertion surface for compose-loop tests."""

        return self.tool_outcomes


# Deterministic advisor checkpoint primitives. The verdict is module-level
# because both service methods and focused unit tests consume it.
