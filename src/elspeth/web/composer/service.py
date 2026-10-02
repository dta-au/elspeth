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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from elspeth.web.composer.redaction_telemetry import RedactionTelemetry
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
from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import (
    ComposerLLMCall,
)
from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
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
    _with_advisor_gate_decision,
)
from elspeth.web.composer.control_messages import anti_anchor_control_envelope
from elspeth.web.composer.discovery_cache import (
    CachedDiscoveryPayload as _CachedDiscoveryPayload,
)
from elspeth.web.composer.discovery_cache import (
    RuntimePreflightCache as _RuntimePreflightCache,
)
from elspeth.web.composer.interpretation_surfacing import InterpretationSurfacing
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.llm_response_parsing import (
    attach_llm_calls,
)
from elspeth.web.composer.planning_application import PlanningApplication
from elspeth.web.composer.progress import (
    convergence_progress_event,
    emit_progress,
    model_call_progress_event,
)
from elspeth.web.composer.prompts import (
    build_messages,
    build_run_diagnostics_messages,
    render_system_prompt,
)
from elspeth.web.composer.protocol import (
    COMPOSER_HISTORY_USER_AUTHORED_KEY,
    COMPOSER_HISTORY_USER_MESSAGE_ID_KEY,
    ComposerConvergenceError,
    ComposerHistoryMessage,
    ComposerPluginCrashError,
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
from elspeth.web.composer.provider_quota import composer_quota_scope
from elspeth.web.composer.reasoning import warn_if_not_reasoning_capable
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
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.execution.completion_gates import (
    CompletionGateFacts,
)
from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
from elspeth.web.execution.schemas import (
    ValidationResult,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.secrets.wiring_policy import runtime_secret_wiring_policy
from elspeth.web.sessions._persist_payload import AuditOutcome, RedactedToolRow

slog = structlog.get_logger()


async def _await_tool_turn_with_deferred_cancellation[T](
    awaitable: Awaitable[T],
    *,
    cancellation_requested: asyncio.Event,
) -> tuple[T, asyncio.CancelledError | None]:
    """Finish one dispatch+persist critical section before cancelling.

    The child task is shielded because synchronous tool and persistence
    workers cannot be stopped once submitted. Cancellation is remembered and
    exposed to ``run_tool_batch`` through ``cancellation_requested`` so it
    completes only the in-flight tool, publishes that completed prefix, and
    starts no further tool calls.
    """
    task = asyncio.ensure_future(awaitable)
    deferred: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), deferred
        except _ToolBatchCancellationRequested:
            if deferred is None:
                raise
            raise deferred from None
        except asyncio.CancelledError as exc:
            # A cancellation raised *by* the child remains a real child
            # outcome. ``shield`` only protects it from cancellation of this
            # awaiting task; it does not launder child cancellation.
            if task.cancelled():
                raise
            cancellation_requested.set()
            if deferred is None:
                deferred = exc
            if task.done():
                try:
                    return task.result(), deferred
                except _ToolBatchCancellationRequested:
                    raise deferred from None
                except Exception as child_exc:
                    slog.warning(
                        "tool_turn_child_failed_during_deferred_cancellation",
                        exc_class=type(child_exc).__name__,
                    )
                    raise deferred from child_exc
        except Exception as child_exc:
            # The child failed AFTER a cancellation was caught and deferred.
            # Python never redelivers the caught CancelledError on its own:
            # letting the child exception propagate would finish the route
            # on its error path with the task's cancellation requests still
            # pending — swallowing an operator/shutdown cancel (and the
            # disconnect watcher's except-CancelledError bookkeeping would
            # never run). Cancellation wins; the child failure rides along
            # as ``__cause__`` so the audit/log trail keeps the diagnosis.
            if deferred is None:
                raise
            slog.warning(
                "tool_turn_child_failed_during_deferred_cancellation",
                exc_class=type(child_exc).__name__,
            )
            raise deferred from child_exc


_ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE = _no_tool_policy.ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE
_is_pending_interpretation_handoff = _no_tool_policy.is_pending_interpretation_handoff
_state_is_structurally_empty = _no_tool_policy.state_is_structurally_empty
_classify_pipeline_mutation_intent = _no_tool_policy.classify_pipeline_mutation_intent
_PipelineMutationIntentDecision = _no_tool_policy.PipelineMutationIntentDecision


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


def _tool_batch_ends_with_valid_current_preview(tool_outcomes: tuple[_ToolOutcome, ...], state: CompositionState) -> bool:
    """Admit terminal verification from this batch's actual current-state result."""
    if not tool_outcomes:
        return False
    for outcome in tool_outcomes:
        if outcome.error_class is not None or not isinstance(outcome.response, ToolResult) or not outcome.response.success:
            return False
    terminal = tool_outcomes[-1]
    response = terminal.response
    return (
        terminal.call.function.name == "preview_pipeline"
        and isinstance(response, ToolResult)
        and response.updated_state == state
        and response.validation.is_valid
        and response.runtime_preflight is not None
        and response.runtime_preflight.is_valid
    )


def _reply_only_messages(llm_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain historical tool evidence as quoted data when no tools are advertised."""
    return [
        (
            {
                "role": "user" if historical_message["role"] == "tool" else historical_message["role"],
                "content": "Historical tool protocol record (quoted data):\n"
                + json.dumps(historical_message, ensure_ascii=False, sort_keys=True),
            }
            if historical_message["role"] == "tool" or "tool_calls" in historical_message
            else historical_message
        )
        for historical_message in llm_messages
    ]


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

    KNOWN REMAINING OVERLAP, deliberately not fixed here: the tail's
    state-claim GROUNDING correction can also co-occur with this announcement
    (green-or-unknown preflight plus contradicting prose), and those two
    suffixes still stack. It is not this branch's defect — the shared tail has
    the same overlap on the pending-handoff shape it owns — and unlike the red
    arm the two carry orthogonal facts, so folding them needs a genuinely new
    composed canonical shape, which is the case
    ``no_tool_finalize.finalize_no_tool_response`` already documents as
    deferred ("a naively concatenated suffix would fail closed ... Composing
    therefore needs a new canonical shape, not a bigger f-string").
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
    """Carry exact, durable human input evidence through mid-turn state writes."""
    inputs: list[ChatIngressInput] = []
    for history_message in messages:
        if type(history_message) is not dict:
            raise InvariantError("composer chat history entries must be exact dictionaries")
        if COMPOSER_HISTORY_USER_AUTHORED_KEY not in history_message:
            continue
        if history_message[COMPOSER_HISTORY_USER_AUTHORED_KEY] is not True or history_message["role"] != "user":
            raise InvariantError("composer user-authorship marker is malformed")
        if COMPOSER_HISTORY_USER_MESSAGE_ID_KEY not in history_message:
            raise AuditIntegrityError("persisted human chat history is missing its message id")
        inputs.append(
            chat_ingress_input(
                history_message[COMPOSER_HISTORY_USER_MESSAGE_ID_KEY],
                history_message["content"],
                own_compartment_id=own_compartment_id,
            )
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
    """LLM-driven pipeline composer with dual-counter budget and discovery caching.

    Runs a bounded tool-use loop with separate budgets for discovery
    and composition turns. Cacheable discovery tool results are cached
    per-compose-call in a local dict (not an instance field) to avoid
    concurrent-request races.

    Budget classification: a turn containing at least one mutation tool
    call charges the composition budget. A turn containing only discovery
    tool calls charges the discovery budget. Cache hits do not charge
    any budget.

    Args:
        catalog: CatalogService for discovery tool delegation.
        settings: ComposerSettings with composer_max_composition_turns,
            composer_max_discovery_turns, composer_timeout_seconds,
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
        self._max_composition_turns = settings.composer_max_composition_turns
        self._max_discovery_turns = settings.composer_max_discovery_turns
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
        self._planning_application = PlanningApplication(
            sessions_service=sessions_service,
            policy_context=self._policy_context,
            chargeable_admission=self._chargeable_admission,
            preflight=self._preflight,
            schema_disclosure=self._schema_disclosure,
            settings=settings,
            availability=self._availability,
            planner_dialect=self._planner_dialect,
            hatch_dialect=self._hatch_dialect,
            advisor_provider=self._advisor_provider,
            composer_skill_text=self._composer_skill_text,
            session_engine=session_engine,
            secret_service=secret_service,
            secret_wiring_policy=self._secret_wiring_policy,
            endpoint_base_url=self._endpoint_base_url,
            endpoint_api_key=self._endpoint_api_key,
            advisor_endpoint_base_url=self._advisor_endpoint_base_url,
            advisor_endpoint_api_key=self._advisor_endpoint_api_key,
        )
        # F-5c gate: ensures the first ``compose()`` call upserts
        # the skill markdown into ``skill_markdown_history`` exactly once
        # per service instance. Subsequent compose() calls observe the
        # flag set and skip the upsert.
        self._skill_markdown_history_upserted: bool = False

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
        result = await self._compose_loop(
            message,
            [],
            state,
            session_id=resolved_session_id,
            initial_current_state_id=current_state_id,
            deadline=asyncio.get_event_loop().time() + self._timeout_seconds,
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            session_operation_context=session_operation_context,
            diagnostics=diagnostics,
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
        with composer_quota_scope(self._require_sessions_service(), session_operation_context):
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
                    timeout=self._timeout_seconds,
                    recorder=recorder,
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

        await self._chargeable_admission.require(session_operation_context)
        with composer_quota_scope(self._require_sessions_service(), session_operation_context):
            deadline = asyncio.get_event_loop().time() + self._timeout_seconds
            # One recorder spans planning or the ordinary loop so every provider
            # and discovery audit for this request is accounted for.
            recorder = BufferingRecorder()
            plugin_snapshot, policy_catalog = self._policy_context.build(user_id)
            try:
                # Which authoring surface a request gets is decided here, and the
                # two are not equivalent: the planner is one bounded call, the
                # compose loop is an iterative turn/wall-clock budget. Nothing else
                # records the choice — the `surface` dimension on Composer
                # telemetry records the authoring choice, not a session mode —
                # so without this line a session's posture cannot be reconstructed
                # after the fact (elspeth-7da4e52344). Booleans and closed vocab
                # only: the message itself is Tier-3 authored text and must not be
                # logged.
                state_is_empty = _state_is_structurally_empty(state)
                # Short-circuit on state_is_empty exactly as the original inline
                # condition did, so the classifier stays a first-turn-only cost
                # rather than running on every compose request. None in the log
                # means "not evaluated" — a non-empty state already decided this.
                intent_is_explicit_mutation = (
                    _classify_pipeline_mutation_intent(message) is _PipelineMutationIntentDecision.EXPLICIT_MUTATION
                    if state_is_empty
                    else None
                )
                planner_eligible = (
                    state_is_empty
                    and intent_is_explicit_mutation is True
                    and self._sessions_service is not None
                    and session_id is not None
                    and user_message_id is not None
                )
                slog.info(
                    "composer_authoring_surface_selected",
                    authoring_surface="planner" if planner_eligible else "compose_loop",
                    state_is_structurally_empty=state_is_empty,
                    intent_is_explicit_mutation=intent_is_explicit_mutation,
                    session_id=session_id,
                )
                # Repeated rather than branching on ``planner_eligible`` so the
                # ``is not None`` conjuncts narrow ``session_id`` /
                # ``user_message_id`` for the call below. Every conjunct here is a
                # cached boolean or a None check — the classifier does not re-run.
                if (
                    state_is_empty
                    and intent_is_explicit_mutation is True
                    and self._sessions_service is not None
                    and session_id is not None
                    and user_message_id is not None
                ):
                    return await self._planning_application._plan_and_stage_empty_pipeline(
                        message=message,
                        session_operation_context=session_operation_context,
                        messages=messages,
                        state=state,
                        session_id=session_id,
                        current_state_id=current_state_id,
                        user_id=user_id,
                        progress=progress,
                        user_message_id=user_message_id,
                        recorder=recorder,
                        plugin_snapshot=plugin_snapshot,
                        policy_catalog=policy_catalog,
                    )
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
                )
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
        completion = await self._call_llm_before_deadline(
            llm_messages,
            tools,
            state,
            initial_version,
            deadline,
            recorder=recorder,
            composition_turns_used=composition_turns_used,
            discovery_turns_used=discovery_turns_used,
            failed_turn=failed_turn,
        )
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

        return _CallModelOutcome(completion=completion)

    async def _persist_turn_audit(
        self,
        *,
        tool_outcomes: tuple[_ToolOutcome, ...],
        decoded_args_by_call_id: Mapping[str, Mapping[str, Any]],
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
            decoded_args_by_call_id=decoded_args_by_call_id,
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
        )

    async def _dispatch_tool_batch(
        self,
        *,
        call_model: _CallModelOutcome,
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
            cancellation_requested=cancellation_requested,
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            tool_contract_dialect=self._planner_dialect,
            session_operation_authority=(turn_sessions_service.session_operation_authority if turn_sessions_service is not None else None),
            composition_turns_used=composition_turns_used,
            discovery_turns_used=discovery_turns_used,
            failed_turn=failed_turn,
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
    ) -> _ClassifyOutcome:
        """Phase P5 of the compose loop — anti-anchor + budget classify.

        Three concerns share this phase because their decision flow is
        sequential:

        1. **Anti-anchor hint (§7.7).** When the last three failed tool
           calls share the same (tool_name, arguments_hash), inject a
           role="user" hint into ``llm_messages`` so the model breaks
           the anchored retry. The hint is persisted via the normal
           ``chat_messages`` path.
        2. **Cache-hit short-circuit.** When every tool call this turn
           was a discovery cache hit, no budget charge: continue.
        3. **Budget classify.** Charge the composition or discovery counter.
           Composition exhaustion gets the B-4D-3 last-chance call; discovery
           exhaustion gets a reply-only call only after a valid current preview.
           Advisor-only turns return to the driver without charging.

        Returns:
            ``_ClassifyOutcome(action="continue", composition_turns_delta=...,
            discovery_turns_delta=...)`` on the normal path, or
            ``_ClassifyOutcome(action="return", result=...)`` when the
            B-4D-3 bonus call terminated the loop. Convergence raises
            (composition / discovery budget exhausted without bonus
            success) leave through the exception channel.
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

        # §7.7 anti-anchor hint: if the last 3 failed tool calls share the
        # same (tool_name, arguments_hash), the model has stopped reading
        # validator feedback. Inject a synthetic role="user" hint before
        # the next LLM turn so the model breaks the anchor. consume_fire()
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
            is_drift_hint = "drift without convergence" in hint_text
            await emit_progress(
                progress,
                ComposerProgressEvent(
                    phase="using_tools",
                    headline="ELSPETH detected a no-progress retry pattern.",
                    evidence=(
                        (
                            "The last 3 tool calls used different arguments but failed the same repair loop."
                            if is_drift_hint
                            else "The last 3 tool calls used identical arguments and produced the same error."
                        ),
                        "A structural hint was injected to help the model converge.",
                    ),
                    likely_next="The model will see the hint and try a different argument shape.",
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
                reply_messages = _reply_only_messages(llm_messages)
                reply_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Review cards have been staged. This is a reply-only turn: tools are unavailable and the pipeline "
                            "must not change. Answer the user's actual question directly using the accepted tool results above. "
                            "Explain the design choices, actual model/profile and failure handling, including any dropped rows. "
                            "Do not call reworded prompts verbatim. Do not claim execution readiness or that approval is the "
                            "only remaining step; the backend will append the current review and validation status."
                        ),
                    }
                )
                provider_failures: tuple[type[Exception], ...] = (TimeoutError, _BadRequestLLMError, *advisor_provider_failure_types())
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
                            reply_messages, [], timeout=remaining, recorder=recorder
                        )
                    except provider_failures:
                        # The call audit retains the failure; a trusted notice
                        # below reports the unavailable reply to the user.
                        reply = None
                    else:
                        reply = completion.message
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
                    _announce_staged_review_handoff(result, reply.content if reply is not None else "")
                    if (result.runtime_preflight is None or not _is_pending_interpretation_handoff(result.runtime_preflight))
                    else result
                )
                handoff_result = _with_advisor_gate_decision(handoff_result, completion_gates, None)
                if reply is None:
                    handoff_result = replace(
                        handoff_result,
                        message=_no_tool_policy.compose_review_reply_unavailable_message(handoff_result.message),
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
                )

        # Classify turn and charge the appropriate budget.
        # The current turn has already been executed (tool results
        # are in the message history). We increment first, then
        # check whether the budget is now exhausted. If so, we give
        # the LLM one last chance (B-4D-3) for composition. A final valid
        # preview can also exhaust discovery after earlier repairs completed:
        # let the model consume that verification in one reply-only call.
        final_preview_at_discovery_cap = (
            not turn_has_mutation
            and turn_has_discovery
            and discovery_turns_used + 1 >= self._max_discovery_turns
            and _tool_batch_ends_with_valid_current_preview(dispatch.tool_outcomes, state)
        )
        if turn_has_mutation or final_preview_at_discovery_cap:
            new_composition_turns_used = composition_turns_used + int(turn_has_mutation)
            new_discovery_turns_used = discovery_turns_used + int(final_preview_at_discovery_cap)
            exhausted_budget: Literal["composition", "discovery"] = "discovery" if final_preview_at_discovery_cap else "composition"
            if new_composition_turns_used >= self._max_composition_turns or final_preview_at_discovery_cap:
                # B-4D-3 fix: give the LLM one last chance to see the
                # tool results and produce a text response.
                final_messages = llm_messages
                final_tools = tools
                if final_preview_at_discovery_cap:
                    final_messages = _reply_only_messages(llm_messages)
                    final_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "The current pipeline's final preview passed validation and the discovery budget is spent. "
                                "This is a reply-only turn: tools are unavailable and the pipeline must not change. "
                                "Answer the user's request using the accepted tool results above. Report what was validated "
                                "without claiming that the pipeline was executed. Remaining completion checks still apply."
                            ),
                        }
                    )
                    final_tools = []
                await emit_progress(progress, model_call_progress_event(message))
                completion = await self._call_llm_before_deadline(
                    final_messages,
                    final_tools,
                    state,
                    initial_version,
                    deadline,
                    recorder=recorder,
                    # The relevant counter has already been charged; a timeout
                    # on the terminal call must report that same total.
                    composition_turns_used=new_composition_turns_used,
                    discovery_turns_used=new_discovery_turns_used,
                    failed_turn=failed_turn,
                )
                self._enforce_tool_call_cap(
                    assistant_tool_calls=completion.tool_batch.calls,
                    state=state,
                    initial_version=initial_version,
                    composition_turns_used=new_composition_turns_used,
                    discovery_turns_used=new_discovery_turns_used,
                    recorder=recorder,
                    failed_turn=failed_turn,
                    persisted_tool_call_turn=persisted_tool_call_turn,
                )
                assistant_message = completion.message
                if not completion.tool_batch.calls:
                    try:
                        advisor_gate = await self._completion._evaluate_terminal_no_tool_advisor_gate(
                            state=state,
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
                            max_turns=new_composition_turns_used + new_discovery_turns_used,
                            budget_exhausted="timeout",
                            state=state,
                            initial_version=initial_version,
                            tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                            llm_calls=recorder.llm_calls,
                            failed_turn=failed_turn,
                        ) from None
                    if advisor_gate.action == "return":
                        return _ClassifyOutcome(
                            action="return",
                            result=advisor_gate.result,
                            composition_turns_delta=int(turn_has_mutation),
                            discovery_turns_delta=int(final_preview_at_discovery_cap),
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
                    result = _with_advisor_gate_decision(result, completion_gates, advisor_gate.advisor_gate_decision)
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
                        composition_turns_delta=int(turn_has_mutation),
                        discovery_turns_delta=int(final_preview_at_discovery_cap),
                    )
                raise ComposerConvergenceError.capture(
                    max_turns=new_composition_turns_used + new_discovery_turns_used,
                    budget_exhausted=exhausted_budget,
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                )
            return _ClassifyOutcome(action="continue", composition_turns_delta=1)
        if turn_has_discovery:
            new_discovery_turns_used = discovery_turns_used + 1
            if new_discovery_turns_used >= self._max_discovery_turns:
                raise ComposerConvergenceError.capture(
                    max_turns=composition_turns_used + new_discovery_turns_used,
                    budget_exhausted="discovery",
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                )
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
        session_operation_context: SessionOperationContext | None = None,
        # Durable advisor gate fact from the prior state row (ruling
        # 2026-09-22). ``None`` = none known: the END gate reviews as before.
        completion_gates: CompletionGateFacts | None = None,
        diagnostics: _ComposeLoopDiagnostics | None = None,
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
        )
        tools = composer_loop_tool_definitions(self._planner_dialect)
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

        while True:
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
            )
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
                    if advisor_repair_context_introduced:
                        return await self._completion._qualified_advisor_repair_public_result(
                            terminate.result,
                            user_id=user_id,
                            session_id=session_id,
                            session_operation_context=session_operation_context,
                            cache=runtime_preflight_cache,
                            initial_version=initial_version,
                            session_scope=session_scope,
                            plugin_snapshot=plugin_snapshot,
                        )
                    return terminate.result
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
            ) -> tuple[_DispatchOutcome, _PersistOutcome, int, bool, bool]:
                dispatch_result, updated_advisor_calls_used = await self._dispatch_tool_batch(
                    call_model=_call_model,
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
                )
                if diagnostics is not None:
                    diagnostics.tool_outcomes = dispatch_result.tool_outcomes
                    diagnostics.pre_state_id = dispatch_result.pre_state_id

                # Do not start a new advisory call after cancellation. If an
                # advisory call was already running when cancellation landed,
                # the enclosing shield lets it finish and P4 still publishes
                # the completed audit prefix.
                early_advisor_message_count = len(llm_messages)
                early_checkpoint_deadline_expired = False
                try:
                    if (
                        not _cancellation_requested.is_set()
                        and dispatch_result.advisor_compose_timeout is None
                        and dispatch_result.advisor_failure is None
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
                            )
                        except _AdvisorCheckpointComposeDeadlineExpired:
                            # The driver converts this signal after persistence
                            # and after plugin/cancellation primacy checks.
                            early_checkpoint_deadline_expired = True
                finally:
                    # The tool batch already ran. Its audit must be persisted
                    # even when an internal advisor failure unwinds this turn.
                    # A successful tool remains successful; the separate
                    # checkpoint failure propagates after this mandatory work.
                    early_advisor_context_introduced = len(llm_messages) > early_advisor_message_count
                    persist_result = await self._persist_turn_audit(
                        tool_outcomes=dispatch_result.tool_outcomes,
                        decoded_args_by_call_id=dispatch_result.decoded_args_by_call_id,
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
                )

            (
                (
                    dispatch,
                    persist,
                    advisor_calls_used,
                    early_advisor_context_introduced,
                    early_checkpoint_deadline_expired,
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
            state = dispatch.state
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
            if pending_advisor_elision_indices and dispatch.mutation_success_observed:
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
                        raise persisted_plugin_crash
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
                        raise unpersisted_plugin_crash
                    raise unpersisted_plugin_crash from dispatch.plugin_crash_cause
                # No session/audit target was configured, so nothing in the
                # request-cumulative crash carrier has been persisted yet.
                if dispatch.plugin_crash_cause is None:
                    raise dispatch.plugin_crash
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
                raise dispatch.advisor_failure

            if deferred_cancel is not None:
                # P3's in-flight tool and P4's atomic audit publication are
                # now complete. Preserve any LLM-call audit carried by this
                # compose request, then resume the original cancellation at
                # the first safe checkpoint before P5 or another model turn.
                attach_llm_calls(deferred_cancel, recorder)
                raise deferred_cancel

            if dispatch.advisor_compose_timeout is not None:
                current_invocation_count = len(dispatch.tool_outcomes)
                if current_invocation_count == 0 or current_invocation_count > len(recorder.invocations):
                    raise InvariantError("advisor timeout dispatch must carry one invocation per current-turn tool outcome")
                raise ComposerConvergenceError.capture(
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

            if early_checkpoint_deadline_expired:
                charged_composition_turns = composition_turns_used + (1 if dispatch.turn_has_mutation else 0)
                charged_discovery_turns = discovery_turns_used + (
                    1 if dispatch.turn_has_discovery and not dispatch.turn_has_mutation else 0
                )
                raise ComposerConvergenceError.capture(
                    max_turns=charged_composition_turns + charged_discovery_turns,
                    budget_exhausted="timeout",
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=() if persisted_tool_call_turn else recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=failed_turn,
                )

            classify = await self._classify_and_budget_turn(
                dispatch=dispatch,
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
                if advisor_repair_context_introduced:
                    return await self._completion._qualified_advisor_repair_public_result(
                        classify.result,
                        user_id=user_id,
                        session_id=session_id,
                        session_operation_context=session_operation_context,
                        cache=runtime_preflight_cache,
                        initial_version=initial_version,
                        session_scope=session_scope,
                        plugin_snapshot=plugin_snapshot,
                    )
                return classify.result
            continue

    def _build_messages(
        self,
        chat_history: list[ComposerHistoryMessage],
        state: CompositionState,
        user_message: str,
        session_id: str | None = None,
        user_id: str | None = None,
        plugin_snapshot: PluginAvailabilitySnapshot | None = None,
        policy_catalog: PolicyCatalogView | None = None,
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
        try:
            return build_messages(
                chat_history=chat_history,
                state=state,
                user_message=user_message,
                catalog=policy_catalog,
                data_dir=self._data_dir,
                plugin_snapshot=plugin_snapshot,
                rendered_skill=self._composer_skill_text,
                schemas_loaded=self._schema_disclosure.schemas_loaded_for_session(session_id),
            )
        except OSError as exc:
            raise ComposerServiceError(f"Failed to load deployment skill ({type(exc).__name__})") from exc

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
    ) -> _AdmittedLLMCompletion:
        """Call the LLM with a per-call timeout derived from the deadline.

        LLM calls are pure network I/O with no side effects, so they
        are safe to cancel via asyncio.wait_for.  If the deadline has
        already passed or the call exceeds the remaining budget, raise
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
                return await self._provider_gateway._call_llm_with_audit(
                    messages,
                    tools,
                    timeout=remaining,
                    recorder=recorder,
                )
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
