"""Per-tool-call dispatch pipeline for the composer compose loop.

Extracted verbatim from ComposerServiceImpl._dispatch_tool_batch (service.py)
to take the single largest method out of the god class. The loop body is
UNCHANGED; only its enclosing context is made explicit via the two carriers
below, replacing the prior nested-closure capture of loop-invariant inputs and
loop-carried accumulators.

Behaviour-preservation contract: every terminal arm's
recorder.record(finish_*) / anti_anchor.record_* / llm_messages.append /
budget-class side-effect is identical to the pre-extraction method. Pinned by
tests/unit/web/composer/test_dispatch_arms_characterization.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import UUID

from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.tool_calls import PROVIDER_TOOL_CALL_ID_MAX_LENGTH
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.blobs.protocol import BlobQuotaExceededError
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedToolBatch,
    _AdmittedToolCall,
    _AdmittedToolFunction,
    _CallModelOutcome,
    _DispatchOutcome,
    _ToolBatchCancellationRequested,
    _ToolOutcome,
)
from elspeth.web.composer._required_paths_validator import (
    _TOOL_REQUIRED_PATHS,
    _find_missing_required_paths,
)
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import (
    BufferingRecorder,
    begin_dispatch,
    begin_dispatch_or_arg_error,
    dispatch_with_audit,
    finish_arg_error,
    finish_plugin_crash,
    finish_success,
    rebind_dispatch_arguments,
)
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.bounded_json import JsonBoundaryError, bounded_json_loads
from elspeth.web.composer.discovery_cache import (
    CachedDiscoveryPayload as _CachedDiscoveryPayload,
)
from elspeth.web.composer.discovery_cache import (
    RuntimePreflightCache as _RuntimePreflightCache,
)
from elspeth.web.composer.discovery_cache import (
    cached_discovery_payload as _cached_discovery_payload,
)
from elspeth.web.composer.discovery_cache import (
    make_cache_key as _make_cache_key,
)
from elspeth.web.composer.discovery_cache import (
    result_from_cached_discovery_payload as _result_from_cached_discovery_payload,
)
from elspeth.web.composer.discovery_cache import (
    serialize_tool_result as _serialize_tool_result,
)
from elspeth.web.composer.discovery_cache import (
    tool_result_mutated_composition_state as _tool_result_mutated_composition_state,
)
from elspeth.web.composer.pipeline_custody import (
    finalize_pipeline_custody,
    inline_custody_audit_projection,
    prepare_pipeline_custody,
)
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PipelineProposal,
    PlannerSurface,
    PresentBase,
    composition_content_hash,
    owned_composition_state_authority,
    owned_composition_state_review_arguments,
)
from elspeth.web.composer.progress import (
    emit_progress,
    tool_batch_progress_event,
    tool_completed_progress_event,
    tool_started_progress_event,
)
from elspeth.web.composer.proposals import build_tool_proposal_summary
from elspeth.web.composer.protocol import (
    ComposerPluginCrashError,
    ComposerRuntimePreflightError,
    ComposerServiceError,
    ToolArgumentError,
)
from elspeth.web.composer.required_controls import (
    merge_required_control_affected_components,
    wire_required_controls,
    wire_required_controls_state,
)
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tool_error_payloads import (
    INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
    unknown_tool_arguments_redaction,
)
from elspeth.web.composer.tool_error_payloads import (
    arg_error_payload as _arg_error_payload,
)
from elspeth.web.composer.tools import (
    _MUTATION_TOOLS,
    RuntimePreflight,
    ToolContext,
    ToolResult,
    build_set_pipeline_candidate,
    execute_tool,
    finalize_tool_result,
    is_approval_required_blob_store_only_mutation_tool,
    is_blob_store_only_mutation_tool,
    is_cacheable_discovery_tool,
    is_discovery_tool,
    is_mutation_tool,
    is_session_aware_tool,
    normalize_tool_result_validation,
)
from elspeth.web.composer.tools._common import _failure_result
from elspeth.web.composer.tools.sessions import canonicalize_authored_node_review_requirements
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

    from elspeth.web.composer.pipeline_custody import PipelineCustodyPreparation
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.sessions.protocol import (
        ComposerSessionPreferencesRecord,
        SessionServiceProtocol,
    )


_MAX_PENDING_PROPOSALS_PER_TURN: Final[int] = 10


_MISSING_TOOL_CALL_FIELD = object()


def _admit_tool_batch(tool_calls: Sequence[Any]) -> _AdmittedToolBatch:
    """Copy and validate provider calls before the first await or side effect.

    Field presence is probed with ``getattr(..., _MISSING_TOOL_CALL_FIELD)``
    and NEVER with a ``runtime_checkable`` ``Protocol`` ``isinstance()``
    check. Since Python 3.12 a runtime-checkable Protocol's
    ``__instancecheck__`` resolves members through
    ``inspect.getattr_static``, which deliberately bypasses ``__getattr__``.
    Real provider tool calls do not survive that: LiteLLM's
    ``litellm.types.utils.ChatCompletionMessageToolCall`` declares NO
    pydantic model fields at all — ``id``/``type``/``function`` live in
    ``__pydantic_extra__`` and resolve only through ``BaseModel.__getattr__``
    — so a Protocol ``isinstance()`` rejects every genuine tool call from
    every provider (elspeth-9ea866438b). Do not "harden" this back into a
    Protocol check.

    The Tier-3 posture is unchanged and lives entirely in the value
    assertions below plus the read-once copy into the frozen
    ``_AdmittedToolCall``/``_AdmittedToolFunction``: a missing field, a
    non-``str`` field, a blank/oversized/duplicate ID are each still a hard
    ``AuditIntegrityError``, and every field is read exactly once and
    snapshotted, so nothing downstream can be re-resolved to a different
    value. What is deliberately NOT asserted is the *mechanism* by which a
    provider object resolves those fields — that is unobservable at this
    boundary once the values are snapshotted, and demanding static
    resolution is precisely what broke against reality.

    ``getattr`` absorbs only ``AttributeError``. A provider object whose
    ``__getattr__`` raises something else propagates that exception rather
    than converting it to ``AuditIntegrityError``; that matches the
    behaviour this guard had before the Protocol experiment, and widening
    to ``except Exception`` would turn this into the permissive duck-type
    the boundary exists to prevent.
    """
    admitted_calls: list[_AdmittedToolCall] = []
    call_ids: set[str] = set()
    for tool_call in tool_calls:
        call_id = getattr(tool_call, "id", _MISSING_TOOL_CALL_FIELD)
        if call_id is _MISSING_TOOL_CALL_FIELD:
            raise AuditIntegrityError("Composer tool batch is missing a provider tool-call ID")
        if type(call_id) is not str:
            raise AuditIntegrityError("Composer tool batch contains a non-string provider tool-call ID")
        if not call_id.strip():
            raise AuditIntegrityError("Composer tool batch contains a blank provider tool-call ID")
        if len(call_id) > PROVIDER_TOOL_CALL_ID_MAX_LENGTH:
            raise AuditIntegrityError("Composer tool batch contains an oversized provider tool-call ID")
        if call_id in call_ids:
            raise AuditIntegrityError("Composer tool batch contains duplicate provider tool-call IDs")

        function = getattr(tool_call, "function", _MISSING_TOOL_CALL_FIELD)
        function_name = getattr(function, "name", _MISSING_TOOL_CALL_FIELD)
        function_arguments = getattr(function, "arguments", _MISSING_TOOL_CALL_FIELD)
        if type(function_name) is not str or type(function_arguments) is not str:
            raise AuditIntegrityError("Composer tool batch contains malformed provider function metadata")

        call_ids.add(call_id)
        admitted_calls.append(
            _AdmittedToolCall(
                id=call_id,
                function=_AdmittedToolFunction(
                    name=function_name,
                    arguments=function_arguments,
                ),
            )
        )
    return _AdmittedToolBatch(
        calls=tuple(admitted_calls),
        call_ids=frozenset(call_ids),
    )


async def _preflight_session_tool_call_ids(
    batch: _AdmittedToolBatch,
    *,
    sessions_service: SessionServiceProtocol | None,
    session_id: UUID | None,
) -> None:
    """Reject IDs already owned by durable rows before current-turn effects."""
    if sessions_service is None or session_id is None:
        return

    prior_messages = await sessions_service.get_messages(session_id, limit=None)
    prior_tool_call_ids = {
        message.tool_call_id for message in prior_messages if message.role == "tool" and message.tool_call_id is not None
    }
    prior_proposals = await sessions_service.list_composition_proposals(session_id)
    prior_tool_call_ids.update(proposal.tool_call_id for proposal in prior_proposals)
    prior_interpretations = await sessions_service.list_interpretation_events(
        session_id,
        status="all",
    )
    prior_tool_call_ids.update(event.tool_call_id for event in prior_interpretations if event.tool_call_id is not None)
    if not batch.call_ids.isdisjoint(prior_tool_call_ids):
        raise AuditIntegrityError("Composer tool batch reuses a provider tool-call ID already persisted in this session")


async def _try_finalize_proposal_custody(
    custody: PipelineCustodyPreparation,
    *,
    engine: Engine,
    data_dir: str | Path,
    max_storage_per_session: int,
) -> Literal["ready", "quota_exceeded"]:
    """Return an explicit quota outcome while preserving other failures."""
    try:
        await finalize_pipeline_custody(
            custody,
            engine=engine,
            data_dir=data_dir,
            max_storage_per_session=max_storage_per_session,
        )
    except BlobQuotaExceededError:
        return "quota_exceeded"
    return "ready"


def _replace_llm_tool_call_arguments(
    llm_messages: Sequence[Mapping[str, Any]],
    *,
    tool_call_id: str,
    arguments: Mapping[str, Any],
) -> None:
    """Replace the latest assistant call with custody-safe arguments.

    The compose loop appends the provider-authored assistant message before
    dispatch.  A subsequent provider turn must not receive raw inline bytes
    from that history after ELSPETH has intercepted them for proposal custody.
    """
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    for message in reversed(llm_messages):
        if "role" not in message or message["role"] != "assistant":
            continue
        tool_calls = message["tool_calls"] if "tool_calls" in message else None
        if type(tool_calls) is not list:
            continue
        for call in tool_calls:
            if type(call) is not dict:
                continue
            if "id" not in call or call["id"] != tool_call_id:
                continue
            function = call["function"] if "function" in call else None
            if type(function) is not dict:
                raise AuditIntegrityError("Assistant tool call has malformed function envelope")
            function["arguments"] = encoded
            return
    raise AuditIntegrityError("Assistant tool call was not present in the active LLM transcript")


@dataclass(frozen=True, slots=True)
class _SetPipelineFinalization:
    """Pure candidate-finalization outcome before custody or publication."""

    arguments: Mapping[str, Any]
    context: ToolContext
    candidate: Any
    changed: bool


async def _finalize_complete_set_pipeline_candidate(
    arguments: Mapping[str, Any],
    state: CompositionState,
    context: ToolContext,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    policy_catalog: PolicyCatalogView,
) -> _SetPipelineFinalization:
    """Auto-wire only an already-acceptable atomic set_pipeline draft."""
    candidate = await run_sync_in_worker(
        build_set_pipeline_candidate,
        arguments,
        state,
        context,
    )
    if not candidate.acceptable:
        return _SetPipelineFinalization(arguments, context, candidate, False)

    finalized = await run_sync_in_worker(
        wire_required_controls,
        arguments,
        plugin_snapshot,
        policy_catalog,
    )
    if finalized is arguments:
        return _SetPipelineFinalization(arguments, context, candidate, False)

    detached = deep_thaw(finalized)
    if type(detached) is not dict:
        raise AuditIntegrityError("Required-control finalization must return an exact argument mapping")
    compact_arguments = cast(dict[str, Any], detached)

    # First admit the nominal server-staged compact disclosure. This preserves
    # the public-boundary rejection of a provider-authored plain-string copy of
    # the reserved term.
    compact_candidate = await run_sync_in_worker(
        build_set_pipeline_candidate,
        compact_arguments,
        state,
        context,
    )
    if not compact_candidate.acceptable:
        raise AuditIntegrityError("Required-control finalization produced an unacceptable compact candidate")

    canonical_arguments = canonicalize_authored_node_review_requirements(
        compact_arguments,
        current_state=state,
    )
    audit_arguments = inline_custody_audit_projection(canonical_arguments)
    internal_context = replace(
        context,
        tool_arguments_hash=composer_authority_hash(audit_arguments),
        _interpretation_requirements_are_internal=True,
    )
    canonical_candidate = await run_sync_in_worker(
        build_set_pipeline_candidate,
        canonical_arguments,
        state,
        internal_context,
    )
    if not canonical_candidate.acceptable:
        raise AuditIntegrityError("Required-control finalization produced an unacceptable set_pipeline candidate")
    return _SetPipelineFinalization(
        canonical_arguments,
        internal_context,
        canonical_candidate,
        True,
    )


async def _finalize_completed_incremental_mutation(
    result: ToolResult,
    prior_state: CompositionState,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
    policy_catalog: PolicyCatalogView,
) -> ToolResult:
    """Wire controls when one incremental mutation makes the graph valid.

    The mutation has already completed in memory, but its audit success and P4
    state publication have not happened yet. Invalid/half-built results remain
    untouched; owned state is finalized directly so public set_pipeline schema
    limitations cannot become a fail-open path.
    """
    if not result.success or result.updated_state.version <= prior_state.version or not result.validation.is_valid:
        return result
    finalized_state = await run_sync_in_worker(
        wire_required_controls_state,
        result.updated_state,
        plugin_snapshot,
        policy_catalog,
    )
    if finalized_state is result.updated_state:
        return result
    finalized_validation = await run_sync_in_worker(
        policy_catalog.validate_composition_state,
        finalized_state,
    )
    if not finalized_validation.validation.is_valid:
        raise AuditIntegrityError("Required-control state finalization produced an invalid composition")
    return replace(
        result,
        updated_state=finalized_validation.authored_state,
        validation=finalized_validation.validation,
        affected_nodes=merge_required_control_affected_components(
            result.affected_nodes,
            result.updated_state,
            finalized_validation.authored_state,
        ),
    )


@dataclass(frozen=True, slots=True)
class ToolBatchContext:
    """Loop-invariant inputs to the dispatch loop, built once per batch.

    ``frozen=True`` prevents rebinding the field *references* only. The
    ``discovery_cache`` and ``runtime_preflight_cache`` fields are
    intentionally-mutable shared caches the dispatch loop writes into; they
    are deliberately exempt from the project's ``deep_freeze`` contract
    because deep-freezing them would break the loop's cache-write behaviour.
    No ``__post_init__`` freeze guard is added for that reason.
    """

    service: ComposerServiceImpl
    recorder: BufferingRecorder
    anti_anchor: AntiAnchorTracker
    discovery_cache: dict[str, _CachedDiscoveryPayload]
    runtime_preflight_cache: _RuntimePreflightCache
    session_id: str | None
    user_id: str | None
    user_message_id: str | None
    user_message_content: str | None
    current_state_id: str | None
    actor: str
    initial_version: int
    deadline: float
    progress: ComposerProgressSink | None
    session_scope: str
    turn_sessions_service: SessionServiceProtocol | None
    turn_session_uuid: UUID | None
    turn_preferences: ComposerSessionPreferencesRecord | None
    cancellation_requested: asyncio.Event
    plugin_snapshot: PluginAvailabilitySnapshot
    policy_catalog: PolicyCatalogView


@dataclass(slots=True)
class BatchAccumulator:
    """Driver-owned state threaded across compose-loop turns.

    These four are the only live loop-carried inputs: ``run_tool_batch``
    reads each exactly once (alias preamble) and the driver carries the
    updated values forward via ``_DispatchOutcome``. The batch body keeps
    every other accumulator (``tool_outcomes``, ``turn_has_mutation``, ...)
    as inline locals, so they are NOT fields here — adding them back would
    create a second, unread source of truth shadowing those locals. The
    deferred Phase 3 (locals->carriers) migration is what would move that
    state onto this carrier; until then this is exactly the live surface.
    """

    state: CompositionState
    last_validation: ValidationSummary | None
    last_runtime_preflight: ValidationResult | None
    advisor_calls_used: int


async def run_tool_batch(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
) -> tuple[_DispatchOutcome, int]:
    """Execute one tool batch — extracted verbatim from
    ``ComposerServiceImpl._dispatch_tool_batch``.

    See the module docstring and ``ToolBatchContext`` for the
    behaviour-preservation contract. The body below is the former method
    body with ``self.`` rewritten to ``ctx.service.`` and the
    loop-invariant / driver-owned locals supplied by the alias preamble.
    """
    # ------------------------------------------------------------------
    # Alias preamble: reconstruct the original ``_dispatch_tool_batch``
    # local namespace so the body below is genuinely verbatim. Loop-invariant
    # inputs come from ``ctx``; the four driver-owned loop-carried inputs come
    # from ``acc``. Every other body init (``tool_outcomes = []``,
    # ``turn_has_mutation = False``, ...) stays inline exactly as before, so
    # the closures, the ``_append_tool_outcome`` default-arg capture, and
    # every loop reassignment keep working against the same local names.
    # ``self.`` is rewritten to ``ctx.service.`` — the only token change.
    # ------------------------------------------------------------------
    recorder = ctx.recorder
    anti_anchor = ctx.anti_anchor
    discovery_cache = ctx.discovery_cache
    runtime_preflight_cache = ctx.runtime_preflight_cache
    session_id = ctx.session_id
    user_id = ctx.user_id
    user_message_id = ctx.user_message_id
    user_message_content = ctx.user_message_content
    current_state_id = ctx.current_state_id
    actor = ctx.actor
    initial_version = ctx.initial_version
    deadline = ctx.deadline
    progress = ctx.progress
    session_scope = ctx.session_scope
    turn_sessions_service = ctx.turn_sessions_service
    turn_session_uuid = ctx.turn_session_uuid
    turn_preferences = ctx.turn_preferences
    state = acc.state
    last_validation = acc.last_validation
    last_runtime_preflight = acc.last_runtime_preflight
    advisor_calls_used = acc.advisor_calls_used

    completion = call_model.completion
    assistant_message = completion.message
    raw_assistant_content = assistant_message.content
    admitted_batch = completion.tool_batch
    assistant_tool_calls = admitted_batch.calls
    provider_model_version = completion.provider_metadata.model_returned or ctx.service._model
    if (
        turn_sessions_service is not None
        and turn_session_uuid is not None
        and turn_preferences is not None
        and turn_preferences.trust_mode == "explicit_approve"
        and sum(
            1
            for tool_call in assistant_tool_calls
            if is_mutation_tool(tool_call.function.name)
            and (
                not is_blob_store_only_mutation_tool(tool_call.function.name)
                or is_approval_required_blob_store_only_mutation_tool(tool_call.function.name)
            )
        )
        > _MAX_PENDING_PROPOSALS_PER_TURN
    ):
        raise ComposerServiceError(
            f"Composer produced too many pending tool proposals in one turn ({_MAX_PENDING_PROPOSALS_PER_TURN} maximum)."
        )
    await _preflight_session_tool_call_ids(
        admitted_batch,
        sessions_service=turn_sessions_service,
        session_id=turn_session_uuid,
    )

    await emit_progress(
        progress,
        tool_batch_progress_event(
            tuple(tool_call.function.name for tool_call in assistant_tool_calls),
        ),
    )

    # Append the assistant message (with tool_calls metadata)
    llm_messages.append(
        {
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_tool_calls
            ],
        }
    )

    # Execute each tool call, tracking whether this turn has
    # budgeted discovery or mutation work. Advisor-only turns are
    # intentionally neither: they spend the advisor-specific budget,
    # not the generic discovery/composition turn budgets.
    turn_has_mutation = False
    turn_has_discovery = False
    all_cache_hits = True
    # Step 1 — execute tool calls in async land while accumulating
    # immutable _ToolOutcome records. Step 2 (in _persist_turn_audit)
    # performs audit writes from this list; cancellation before Step 2
    # leaves the DB unchanged for the current assistant response.
    tool_outcomes: list[_ToolOutcome] = []
    plugin_crash: ComposerPluginCrashError | None = None
    plugin_crash_cause: BaseException | None = None
    advisor_compose_timeout: Literal["pre_call", "in_flight"] | None = None
    pre_state_id: str | None = current_state_id
    ctx.service._phase3_last_expected_current_state_id = pre_state_id
    decoded_args_by_call_id: dict[str, dict[str, Any]] = {}
    proposals_this_turn = 0
    mutation_success_observed = False
    from elspeth.web.composer.redaction import MANIFEST

    for tool_call in assistant_tool_calls:
        if ctx.cancellation_requested.is_set():
            # The enclosing compose-loop critical section has observed a
            # cancellation. If no tool started, abort without publishing a
            # fabricated empty assistant turn. Otherwise return the completed
            # prefix so P4 can atomically publish its results before the
            # original cancellation resumes. Never start another tool after
            # the client has gone away.
            if not tool_outcomes:
                raise _ToolBatchCancellationRequested
            break
        tool_name = tool_call.function.name
        pre_version = state.version
        unknown_audit_arguments: dict[str, Any] | None = None
        if tool_name not in MANIFEST:
            if is_discovery_tool(tool_name) or is_mutation_tool(tool_name) or is_session_aware_tool(tool_name):
                raise AuditIntegrityError(f"Registered composer tool {tool_name!r} is missing from the redaction manifest")
            unknown_audit_arguments = cast(
                dict[str, Any],
                unknown_tool_arguments_redaction(telemetry=ctx.service._redaction_telemetry),
            )
            decoded_args_by_call_id[tool_call.id] = unknown_audit_arguments
            _replace_llm_tool_call_arguments(
                llm_messages,
                tool_call_id=tool_call.id,
                arguments=unknown_audit_arguments,
            )

        def _append_tool_outcome(
            *,
            response: Any,
            error_class: str | None,
            error_message: str | None,
            post_version: int,
            _tool_outcomes: list[_ToolOutcome] = tool_outcomes,
            _tool_call: Any = tool_call,
            _pre_version: int = pre_version,
        ) -> None:
            _tool_outcomes.append(
                _ToolOutcome(
                    call=_tool_call,
                    response=response,
                    error_class=error_class,
                    error_message=error_message,
                    pre_version=_pre_version,
                    post_version=post_version,
                )
            )

        try:
            decoded_arguments = bounded_json_loads(tool_call.function.arguments, label=f"{tool_name} arguments")
        except (json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError) as exc:
            # Track budget class even when args are unparseable.
            if is_discovery_tool(tool_name):
                turn_has_discovery = True
            else:
                turn_has_mutation = True
            # ARG_ERROR pre-dispatch site (1/3): JSON-decode failure.
            # Open the audit envelope with the raw (pre-parse) string
            # so the trail records what the LLM tried, even when it
            # wasn't valid JSON. ``error_message`` is class-name only
            # because ``str(exc)`` for JSONDecodeError can echo column
            # offsets that reference the un-truncated raw bytes.
            audit_arguments: Mapping[str, Any] | str = (
                unknown_audit_arguments if unknown_audit_arguments is not None else tool_call.function.arguments
            )
            if isinstance(exc, JsonBoundaryError) and unknown_audit_arguments is None:
                audit_arguments = {
                    "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                    "error_class": type(exc).__name__,
                }
                decoded_args_by_call_id[tool_call.id] = dict(audit_arguments)
                _replace_llm_tool_call_arguments(
                    llm_messages,
                    tool_call_id=tool_call.id,
                    arguments=audit_arguments,
                )
            audit = begin_dispatch(
                tool_call.id,
                tool_name,
                audit_arguments,
                version_before=state.version,
                actor=actor,
            )
            error_payload = {"error": f"Invalid JSON in arguments: {exc}"}
            recorder.record(
                finish_arg_error(
                    audit,
                    error_class=type(exc).__name__,
                    error_message=type(exc).__name__,
                    error_payload=error_payload,
                )
            )
            _append_tool_outcome(
                response=None,
                error_class=type(exc).__name__,
                error_message=type(exc).__name__,
                post_version=state.version,
            )
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(error_payload),
                }
            )
            all_cache_hits = False
            continue

        if not isinstance(decoded_arguments, dict):
            if is_discovery_tool(tool_name):
                turn_has_discovery = True
            else:
                turn_has_mutation = True
            # ARG_ERROR pre-dispatch site (2/3): non-dict arguments.
            # The LLM produced valid JSON but not a JSON object. The
            # canonicalized record wraps the (possibly scalar/list)
            # value under ``_decoded_non_object`` so the audit trail
            # captures it deterministically.
            audit, canonicalization_failed = begin_dispatch_or_arg_error(
                tool_call.id,
                tool_name,
                unknown_audit_arguments if unknown_audit_arguments is not None else {"_decoded_non_object": decoded_arguments},
                version_before=state.version,
                actor=actor,
            )
            if canonicalization_failed is None:
                err_msg = f"Tool '{tool_name}' arguments must be a JSON object, got {type(decoded_arguments).__name__}."
                error_class = "TypeError"
                error_message = f"non-object arguments ({type(decoded_arguments).__name__})"
            else:
                err_msg = f"Tool '{tool_name}' arguments are not canonical JSON ({type(canonicalization_failed).__name__})."
                error_class = type(canonicalization_failed).__name__
                error_message = type(canonicalization_failed).__name__
            error_payload = {"error": err_msg}
            recorder.record(
                finish_arg_error(
                    audit,
                    error_class=error_class,
                    error_message=error_message,
                    error_payload=error_payload,
                )
            )
            _append_tool_outcome(
                response=None,
                error_class=error_class,
                error_message=error_message,
                post_version=state.version,
            )
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(error_payload),
                }
            )
            all_cache_hits = False
            continue

        arguments = cast(dict[str, Any], decoded_arguments)
        if unknown_audit_arguments is not None:
            audit_arguments = unknown_audit_arguments
        elif tool_name == "set_pipeline":
            audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
        else:
            audit_arguments = arguments
        decoded_args_by_call_id[tool_call.id] = audit_arguments

        # Open the audit envelope ONCE per dispatch — the cache,
        # required-paths, ToolArgumentError, plugin-crash, and
        # success branches below all read from this envelope so
        # ``started_at``/``arguments_canonical``/``version_before``
        # are consistent regardless of which branch fires.
        audit, canonicalization_failed = begin_dispatch_or_arg_error(
            tool_call.id,
            tool_name,
            audit_arguments,
            version_before=state.version,
            actor=actor,
        )
        if audit_arguments is not arguments:
            _replace_llm_tool_call_arguments(
                llm_messages,
                tool_call_id=tool_call.id,
                arguments=audit_arguments,
            )
        if canonicalization_failed is not None:
            if is_discovery_tool(tool_name):
                turn_has_discovery = True
            else:
                turn_has_mutation = True
            error_payload = {"error": f"Tool '{tool_name}' arguments are not canonical JSON ({type(canonicalization_failed).__name__})."}
            recorder.record(
                finish_arg_error(
                    audit,
                    error_class=type(canonicalization_failed).__name__,
                    error_message=type(canonicalization_failed).__name__,
                    error_payload=error_payload,
                )
            )
            _append_tool_outcome(
                response=None,
                error_class=type(canonicalization_failed).__name__,
                error_message=type(canonicalization_failed).__name__,
                post_version=state.version,
            )
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(error_payload),
                }
            )
            all_cache_hits = False
            continue

        # Check discovery cache before executing
        if is_cacheable_discovery_tool(tool_name):
            cache_key = _make_cache_key(tool_name, arguments)
            if cache_key in discovery_cache:
                # Cache hit — return cached result, no budget charge.
                # Audit-recorded with cache_hit=True so the trail
                # captures every LLM decision-point, even those
                # served from cache without re-running the handler.
                await emit_progress(
                    progress,
                    ComposerProgressEvent(
                        phase="using_tools",
                        headline="I'm reusing recently checked tool information.",
                        evidence=("The same discovery request was already answered for this compose step.",),
                        likely_next="ELSPETH will continue from the cached tool result.",
                    ),
                )
                cached_result = _result_from_cached_discovery_payload(
                    state,
                    discovery_cache[cache_key],
                )
                cached_result = normalize_tool_result_validation(cached_result, ctx.policy_catalog)
                cached_payload = {
                    "success": cached_result.success,
                    "data": cached_result.data,
                    "cache_hit": True,
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=cached_payload,
                        version_after=state.version,
                        cache_hit=True,
                    )
                )
                _append_tool_outcome(
                    response=cached_result,
                    error_class=None,
                    error_message=None,
                    post_version=state.version,
                )
                # Cache hits are exclusively for discovery tools
                # (`is_cacheable_discovery_tool` gates above). A
                # discovery success is an *observation* — the model
                # gained schema knowledge but did not change state.
                # The §7.7 anchor is broken only by mutation
                # progress, so tracker stays untouched here.
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _serialize_tool_result(cached_result),
                    }
                )
                continue

        all_cache_hits = False

        # Validate schema-declared required arguments at the
        # Tier 3 boundary BEFORE entering tool handler code.
        # This walks nested object/array schemas, so malformed
        # set_pipeline payloads like source.plugin omissions are
        # caught here; any KeyError that still escapes
        # execute_tool() is an internal bug and must crash.
        # Unknown tool names skip validation — execute_tool()
        # handles them with a failure result downstream.
        required_paths = _TOOL_REQUIRED_PATHS[tool_name] if tool_name in _TOOL_REQUIRED_PATHS else ()
        missing = _find_missing_required_paths(arguments, required_paths)
        if missing:
            if is_discovery_tool(tool_name):
                turn_has_discovery = True
            else:
                turn_has_mutation = True
            # ARG_ERROR pre-dispatch site (3/3): schema-required
            # paths missing. ``missing`` is a list of dotted/indexed
            # path strings — operator-controlled schema field names,
            # safe to echo verbatim.
            err_msg = f"Tool '{tool_name}' missing required argument(s): {', '.join(missing)}"
            error_payload = {"error": err_msg}
            recorder.record(
                finish_arg_error(
                    audit,
                    error_class="MissingRequiredPaths",
                    error_message=f"missing: {', '.join(missing)}",
                    error_payload=error_payload,
                )
            )
            _append_tool_outcome(
                response=None,
                error_class="MissingRequiredPaths",
                error_message=f"missing: {', '.join(missing)}",
                post_version=state.version,
            )
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(error_payload),
                }
            )
            continue

        prevalidated_unapplied_result: ToolResult | None = None
        preproposal_exception: BaseException | None = None
        candidate_prior_validation: ValidationSummary | None = None
        candidate_context: ToolContext | None = None
        candidate: Any = None
        pipeline_custody_result: Literal["not_required", "ready"] = "not_required"
        interpretation_requirements_are_internal = False
        explicit_approval_required = (
            turn_sessions_service is not None
            and turn_session_uuid is not None
            and turn_preferences is not None
            and turn_preferences.trust_mode == "explicit_approve"
            and is_mutation_tool(tool_name)
            # create_blob stays immediate because it allocates the blob id a
            # later composition proposal may reference. Destructive blob-only
            # writes such as update_blob/delete_blob still require approval.
            and (not is_blob_store_only_mutation_tool(tool_name) or is_approval_required_blob_store_only_mutation_tool(tool_name))
        )

        # The freeform compose loop is a second authoring surface beside the
        # shared planner. Its atomic set_pipeline call is the equivalent
        # terminal candidate seam: validate the provider draft first, then
        # splice deployment-REQUIRED controls exactly once before proposal
        # custody or auto-commit. Incomplete/invalid drafts remain byte-exact
        # and continue through the ordinary repair path; in particular, the
        # server never launders a provider-authored reserved disclosure into
        # an internal one merely by attempting finalization.
        if tool_name == "set_pipeline" and not explicit_approval_required:
            try:
                candidate_prior_validation = (
                    last_validation if last_validation is not None else ctx.policy_catalog.validate_composition_state(state).validation
                )
                candidate_context = ToolContext(
                    catalog=ctx.policy_catalog,
                    plugin_snapshot=ctx.plugin_snapshot,
                    data_dir=ctx.service._data_dir,
                    require_data_dir_for_paths=True,
                    session_engine=ctx.service._session_engine,
                    session_id=session_id,
                    secret_service=ctx.service._secret_service,
                    user_id=user_id,
                    current_validation=candidate_prior_validation,
                    max_blob_storage_per_session_bytes=ctx.service._settings.max_blob_storage_per_session_bytes,
                    user_message_id=user_message_id,
                    user_message_content=user_message_content,
                    composer_model_identifier=ctx.service._model,
                    composer_model_version=provider_model_version,
                    composer_provider=ctx.service._availability.provider or "unknown",
                    composer_skill_hash=ctx.service._composer_skill_hash,
                    tool_arguments_hash=audit.binding_arguments_hash,
                )
                finalization = await _finalize_complete_set_pipeline_candidate(
                    arguments,
                    state,
                    candidate_context,
                    plugin_snapshot=ctx.plugin_snapshot,
                    policy_catalog=ctx.policy_catalog,
                )
                arguments = cast(dict[str, Any], finalization.arguments)
                candidate_context = finalization.context
                candidate = finalization.candidate
                if finalization.changed:
                    audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
                    audit = rebind_dispatch_arguments(audit, audit_arguments)
                    decoded_args_by_call_id[tool_call.id] = audit_arguments
                    _replace_llm_tool_call_arguments(
                        llm_messages,
                        tool_call_id=tool_call.id,
                        arguments=audit_arguments,
                    )
                    interpretation_requirements_are_internal = True
            except BaseException as exc:
                # Re-raise exactly once from inside dispatch_with_audit so the
                # established ARG_ERROR / PLUGIN_CRASH authority records the
                # failure without any proposal, blob, or state publication.
                preproposal_exception = exc

        if explicit_approval_required:
            assert turn_sessions_service is not None
            assert turn_session_uuid is not None
            if proposals_this_turn >= _MAX_PENDING_PROPOSALS_PER_TURN:
                raise ComposerServiceError(
                    f"Composer produced too many pending tool proposals in one turn ({_MAX_PENDING_PROPOSALS_PER_TURN} maximum)."
                )

            from pydantic import ValidationError as PydanticValidationError

            from elspeth.web.composer.redaction import redact_tool_call_arguments

            # The LLM may produce arguments that fail the redaction
            # MANIFEST's argument_model — most commonly a misplaced
            # field like ``nodes[*].schema`` belonging at
            # ``nodes[*].options.schema``. The runtime validator
            # would reject these the same way. On redaction
            # ValidationError we fall through to normal dispatch:
            # execute_tool emits a clean ToolArgumentError, the
            # existing post-dispatch arg-error handling records
            # the failure for audit (with the
            # ``_redaction_status: invalid_tool_arguments`` marker),
            # and the compose loop continues so the model can
            # self-correct. The previous behaviour — letting the
            # ValidationError propagate — crashed the compose
            # request with HTTP 500 (session 100dc5cb… 2026-05-14:
            # frontend rendered a generic ApiError as a bare
            # "retry" button with no diagnostic).
            redacted_arguments: Mapping[str, Any] | None
            try:
                redacted_arguments = redact_tool_call_arguments(
                    tool_name,
                    arguments,
                    telemetry=ctx.service._redaction_telemetry,
                )
            except PydanticValidationError:
                redacted_arguments = None

            proposal_tool_name = tool_name
            proposal_arguments: Mapping[str, Any] = arguments
            proposal_summary_arguments: Mapping[str, Any] = arguments
            proposal_redacted_arguments = redacted_arguments

            if redacted_arguments is not None and tool_name == "set_pipeline":
                if preproposal_exception is not None:
                    redacted_arguments = None
                else:
                    try:
                        candidate_prior_validation = (
                            last_validation
                            if last_validation is not None
                            else ctx.policy_catalog.validate_composition_state(state).validation
                        )
                        candidate_context = ToolContext(
                            catalog=ctx.policy_catalog,
                            plugin_snapshot=ctx.plugin_snapshot,
                            data_dir=ctx.service._data_dir,
                            require_data_dir_for_paths=True,
                            session_engine=ctx.service._session_engine,
                            session_id=session_id,
                            secret_service=ctx.service._secret_service,
                            user_id=user_id,
                            current_validation=candidate_prior_validation,
                            max_blob_storage_per_session_bytes=ctx.service._settings.max_blob_storage_per_session_bytes,
                            user_message_id=user_message_id,
                            user_message_content=user_message_content,
                            composer_model_identifier=ctx.service._model,
                            composer_model_version=provider_model_version,
                            composer_provider=ctx.service._availability.provider or "unknown",
                            composer_skill_hash=ctx.service._composer_skill_hash,
                            tool_arguments_hash=audit.binding_arguments_hash,
                        )
                        finalization = await _finalize_complete_set_pipeline_candidate(
                            arguments,
                            state,
                            candidate_context,
                            plugin_snapshot=ctx.plugin_snapshot,
                            policy_catalog=ctx.policy_catalog,
                        )
                        arguments = cast(dict[str, Any], finalization.arguments)
                        candidate_context = finalization.context
                        candidate = finalization.candidate
                        if finalization.changed:
                            audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
                            audit = rebind_dispatch_arguments(audit, audit_arguments)
                            decoded_args_by_call_id[tool_call.id] = audit_arguments
                            _replace_llm_tool_call_arguments(
                                llm_messages,
                                tool_call_id=tool_call.id,
                                arguments=audit_arguments,
                            )
                            interpretation_requirements_are_internal = True
                            redacted_arguments = redact_tool_call_arguments(
                                tool_name,
                                arguments,
                                telemetry=ctx.service._redaction_telemetry,
                            )
                        finalized_candidate_result = finalize_tool_result(
                            candidate.result,
                            tool_name=tool_name,
                            catalog=ctx.policy_catalog,
                            context=candidate_context,
                            prior_validation=candidate_prior_validation,
                        )
                        proposal_acceptable = candidate.acceptable
                        if proposal_acceptable and candidate.prepared_inline_blob is not None:
                            if session_id is None or ctx.service._session_engine is None:
                                raise AuditIntegrityError("Inline proposal custody requires session context")
                            custody = prepare_pipeline_custody(
                                arguments,
                                candidate.prepared_inline_blob,
                                session_id=session_id,
                                max_storage_per_session=ctx.service._settings.max_blob_storage_per_session_bytes,
                            )

                            # From this point forward every authority-bearing
                            # and externally visible copy uses source.blob_id.
                            arguments = cast(dict[str, Any], deep_thaw(custody.arguments))
                            audit = rebind_dispatch_arguments(audit, arguments)
                            decoded_args_by_call_id[tool_call.id] = arguments
                            _replace_llm_tool_call_arguments(
                                llm_messages,
                                tool_call_id=tool_call.id,
                                arguments=arguments,
                            )
                            safe_candidate_context = replace(
                                candidate_context,
                                tool_arguments_hash=audit.binding_arguments_hash,
                            )
                            custody_outcome = await _try_finalize_proposal_custody(
                                custody,
                                engine=ctx.service._session_engine,
                                data_dir=ctx.service._data_dir,
                                max_storage_per_session=ctx.service._settings.max_blob_storage_per_session_bytes,
                            )
                            if custody_outcome == "quota_exceeded":
                                proposal_acceptable = False
                                finalized_candidate_result = finalize_tool_result(
                                    _failure_result(
                                        state,
                                        "Session blob quota exceeded while reserving inline proposal custody.",
                                        error_code="BLOB_QUOTA_EXCEEDED",
                                    ),
                                    tool_name=tool_name,
                                    catalog=ctx.policy_catalog,
                                    context=safe_candidate_context,
                                    prior_validation=candidate_prior_validation,
                                )
                            else:
                                pipeline_custody_result = "ready"
                                candidate = await run_sync_in_worker(
                                    build_set_pipeline_candidate,
                                    arguments,
                                    state,
                                    safe_candidate_context,
                                )
                                finalized_candidate_result = finalize_tool_result(
                                    candidate.result,
                                    tool_name=tool_name,
                                    catalog=ctx.policy_catalog,
                                    context=safe_candidate_context,
                                    prior_validation=candidate_prior_validation,
                                )
                                proposal_acceptable = candidate.acceptable

                            # Re-run the manifest against the final safe shape.
                            redacted_arguments = redact_tool_call_arguments(
                                tool_name,
                                arguments,
                                telemetry=ctx.service._redaction_telemetry,
                            )
                    except BaseException as exc:
                        # Candidate finalization is one-time pre-proposal work.
                        # Re-raise it inside dispatch_with_audit below without
                        # rerunning the operation.
                        preproposal_exception = exc
                        redacted_arguments = None
                    else:
                        if not proposal_acceptable:
                            if isinstance(finalized_candidate_result.data, Mapping):
                                feedback_data = dict(finalized_candidate_result.data)
                            elif finalized_candidate_result.data is None:
                                feedback_data = {}
                            else:
                                feedback_data = {"candidate_data": finalized_candidate_result.data}
                            feedback_data.update(
                                {
                                    "status": "PREVALIDATION_REJECTED",
                                    "applied": False,
                                    "applied_version": state.version,
                                    "candidate_version": finalized_candidate_result.updated_state.version,
                                    "message": (
                                        "The candidate pipeline failed prevalidation, was not applied, and was not "
                                        "submitted for approval. Repair the reported validation errors and retry."
                                    ),
                                }
                            )
                            prevalidated_unapplied_result = replace(
                                finalized_candidate_result,
                                data=feedback_data,
                            )
                            # Route the rejected result through canonical
                            # dispatch/outcome without proposal publication.
                            redacted_arguments = None

            proposal_redacted_arguments = redacted_arguments
            if redacted_arguments is not None and tool_name in _MUTATION_TOOLS and tool_name != "set_pipeline":
                try:
                    preview_result = await run_sync_in_worker(
                        execute_tool,
                        tool_name,
                        arguments,
                        state,
                        ctx.policy_catalog,
                        plugin_snapshot=ctx.plugin_snapshot,
                        data_dir=ctx.service._data_dir,
                        session_engine=ctx.service._session_engine,
                        session_id=session_id,
                        secret_service=ctx.service._secret_service,
                        user_id=user_id,
                        prior_validation=last_validation,
                        runtime_preflight=None,
                        max_blob_storage_per_session_bytes=ctx.service._settings.max_blob_storage_per_session_bytes,
                        user_message_id=user_message_id,
                        user_message_content=user_message_content,
                        composer_model_identifier=ctx.service._model,
                        composer_model_version=provider_model_version,
                        composer_provider=ctx.service._availability.provider or "unknown",
                        composer_skill_hash=ctx.service._composer_skill_hash,
                        tool_arguments_hash=audit.binding_arguments_hash,
                        validate_arguments=True,
                        require_data_dir_for_paths=True,
                        raise_schema_argument_errors=True,
                    )
                    if (
                        preview_result.success
                        and preview_result.updated_state.version > state.version
                        and preview_result.validation.is_valid
                    ):
                        finalized_state = await run_sync_in_worker(
                            wire_required_controls_state,
                            preview_result.updated_state,
                            ctx.plugin_snapshot,
                            ctx.policy_catalog,
                        )
                        if finalized_state is not preview_result.updated_state:
                            finalized_validation = await run_sync_in_worker(
                                ctx.policy_catalog.validate_composition_state,
                                finalized_state,
                            )
                            if not finalized_validation.validation.is_valid:
                                raise AuditIntegrityError("Required-control proposal finalization produced an invalid composition")
                            proposal_arguments = owned_composition_state_authority(finalized_validation.authored_state)
                            proposal_summary_arguments = owned_composition_state_review_arguments(proposal_arguments)
                            proposal_tool_name = "set_pipeline"
                            proposal_redacted_arguments = redact_tool_call_arguments(
                                proposal_tool_name,
                                cast(dict[str, Any], proposal_summary_arguments),
                                telemetry=ctx.service._redaction_telemetry,
                            )
                except ToolArgumentError:
                    # Preserve the established explicit-approval contract for
                    # ordinary incremental proposals: semantic tool-argument
                    # rejection occurs only if the user approves execution.
                    pass
                except BaseException as exc:
                    preproposal_exception = exc
                    proposal_redacted_arguments = None

            if proposal_redacted_arguments is not None and proposal_tool_name == tool_name:
                proposal_arguments = arguments
                proposal_summary_arguments = arguments

            if proposal_redacted_arguments is not None:
                proposal_summary = build_tool_proposal_summary(
                    tool_name=proposal_tool_name,
                    arguments=proposal_summary_arguments,
                    redacted_arguments=proposal_redacted_arguments,
                )
                if proposal_tool_name == "set_pipeline":
                    proposal_base = (
                        PresentBase(
                            state_id=UUID(current_state_id),
                            composition_content_hash=composition_content_hash(state),
                        )
                        if current_state_id is not None
                        else AbsentBase()
                    )
                    pipeline_proposal = PipelineProposal.create(
                        pipeline=proposal_arguments,
                        base=proposal_base,
                        reviewed_facts={},
                        surface=PlannerSurface.FREEFORM,
                        repair_count=0,
                        skill_hash=ctx.service._composer_skill_hash,
                        covered_deferred_intent_ids=(),
                        supersedes_draft_hash=None,
                    )
                    proposal = await turn_sessions_service.create_pipeline_composition_proposal(
                        session_id=turn_session_uuid,
                        plan=PipelinePlanResult(
                            proposal=pipeline_proposal,
                            tool_call_id=tool_call.id,
                            custody_result=pipeline_custody_result,
                            model_identifier=ctx.service._model,
                            model_version=provider_model_version,
                            provider=ctx.service._availability.provider or "unknown",
                        ),
                        summary=proposal_summary.summary,
                        rationale=proposal_summary.rationale,
                        affects=proposal_summary.affects,
                        arguments_redacted_json=proposal_summary.arguments_redacted_json,
                        actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                        user_message_id=UUID(user_message_id) if user_message_id is not None else None,
                        composer_model_identifier=ctx.service._model,
                        composer_model_version=provider_model_version,
                        composer_provider=ctx.service._availability.provider or "unknown",
                    )
                else:
                    proposal = await turn_sessions_service.create_composition_proposal(
                        session_id=turn_session_uuid,
                        tool_call_id=tool_call.id,
                        tool_name=proposal_tool_name,
                        summary=proposal_summary.summary,
                        rationale=proposal_summary.rationale,
                        affects=proposal_summary.affects,
                        arguments_json=proposal_arguments,
                        arguments_redacted_json=proposal_summary.arguments_redacted_json,
                        base_state_id=UUID(current_state_id) if current_state_id is not None else None,
                        actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                        user_message_id=UUID(user_message_id) if user_message_id is not None else None,
                        composer_model_identifier=ctx.service._model,
                        composer_model_version=provider_model_version,
                        composer_provider=ctx.service._availability.provider or "unknown",
                        composer_skill_hash=ctx.service._composer_skill_hash,
                        tool_arguments_hash=audit.binding_arguments_hash,
                    )
                proposals_this_turn += 1
                proposal_payload = {
                    "success": True,
                    "status": "APPROVAL_REQUIRED",
                    "proposal_id": str(proposal.id),
                    "tool_name": proposal_tool_name,
                    "summary": proposal.summary,
                    "message": "The requested pipeline change is pending human approval and has not been applied.",
                }
                proposal_result = ToolResult(
                    success=True,
                    updated_state=state,
                    validation=(
                        candidate_prior_validation
                        if candidate_prior_validation is not None
                        else (
                            last_validation
                            if last_validation is not None
                            else ctx.policy_catalog.validate_composition_state(state).validation
                        )
                    ),
                    affected_nodes=(),
                    data=proposal_payload,
                )
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=proposal_result.to_dict(),
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=proposal_result,
                    error_class=None,
                    error_message=None,
                    post_version=state.version,
                )
                anti_anchor.record_success()
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _serialize_tool_result(proposal_result),
                    }
                )
                turn_has_mutation = True
                continue
            # ``None`` means invalid LLM arguments, an unacceptable
            # prevalidated set_pipeline candidate, or captured one-time
            # pre-proposal work. All fall through to the canonical
            # dispatch/outcome path below.

        await emit_progress(progress, tool_started_progress_event(tool_name))

        # Advisor escape-hatch interception. The request_advisor_hint
        # tool is intercepted here BEFORE execute_tool() because the
        # action is an async LiteLLM call to a frontier model rather
        # than a sync state mutation. The tool is not registered in
        # _DISCOVERY_TOOLS or _MUTATION_TOOLS, so execute_tool() would
        # return "Unknown tool" if this branch did not handle it.
        #
        # Audit envelope was already opened above by
        # begin_dispatch_or_arg_error; this branch closes it with the
        # truthful dispatch status: ARG_ERROR for local advisor-argument
        # rejection, SUCCESS for completed policy/provider outcomes
        # whose semantic status is encoded in result_payload. The inner
        # LLM call is recorded separately via _call_advisor_with_audit
        # firing a ComposerLLMCall record.
        if tool_name == "request_advisor_hint":
            # Successful advisor guidance is governed solely by the
            # advisor budget so the composer can read it. Advisor
            # policy/error feedback with no usable guidance is still
            # a non-mutating correction turn, so it consumes discovery
            # budget before the loop asks the primary model again.
            budget = ctx.service._settings.composer_advisor_max_calls_per_compose
            if advisor_calls_used >= budget:
                budget_payload = {
                    "status": "BUDGET_EXHAUSTED",
                    "budget_used": advisor_calls_used,
                    "budget_remaining": 0,
                    "guidance": (
                        f"Advisor budget exhausted ({advisor_calls_used}/{budget} calls "
                        "used this compose request). Return to the validator output and "
                        "the recovery cheat sheet — no more frontier hints are available "
                        "until the operator raises the budget or the next compose request."
                    ),
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=budget_payload,
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=budget_payload,
                    error_class=None,
                    error_message=None,
                    post_version=state.version,
                )
                # Budget exhaustion is a structural signal back to
                # the LLM, not a tool-failure pattern — do NOT count
                # it for §7.7 anchor tracking, since the issue isn't
                # the LLM repeating an identical request, it's the
                # operator's policy refusing further hints.
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(budget_payload),
                    }
                )
                turn_has_discovery = True
                continue

            # F3: validate argument types and total prompt size at the
            # Tier-3 trust boundary. _TOOL_REQUIRED_PATHS only checks
            # key presence, not value shape. Without this check the
            # LLM could send a non-list (silently iterated char-by-
            # char) or a megabyte-scale value (unbounded provider
            # cost). ARG_ERRORs do NOT consume advisor budget — no
            # outbound call is made — but anti-anchor counts them
            # so repeated identical bad-arg calls trigger the §7.7
            # structural hint.
            advisor_arg_error = ctx.service._validate_advisor_arguments(arguments)
            if advisor_arg_error is not None:
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=str(advisor_arg_error["error_class"]),
                        error_message=str(advisor_arg_error["error"]),
                        error_payload=advisor_arg_error,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=str(advisor_arg_error["error_class"]),
                    error_message=str(advisor_arg_error["error"]),
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(advisor_arg_error),
                    }
                )
                turn_has_discovery = True
                continue

            remaining = _remaining_compose_seconds(deadline)
            if remaining <= 0:
                timeout_payload: dict[str, Any] = {
                    "status": "COMPOSE_TIMEOUT",
                    "error": "Advisor call exceeded the remaining compose deadline.",
                    "error_class": "TimeoutError",
                    "budget_used": advisor_calls_used,
                    "budget_remaining": budget - advisor_calls_used,
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=timeout_payload,
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=timeout_payload,
                    error_class=None,
                    error_message=None,
                    post_version=state.version,
                )
                advisor_compose_timeout = "pre_call"
                break

            elif remaining < _MIN_USEFUL_ADVISOR_SECONDS:
                deadline_payload = {
                    "status": "DEADLINE_TOO_CLOSE",
                    "outbound_call_made": False,
                    "budget_used": advisor_calls_used,
                    "budget_remaining": budget - advisor_calls_used,
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=deadline_payload,
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=deadline_payload,
                    error_class=None,
                    error_message=None,
                    post_version=state.version,
                )
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(deadline_payload),
                    }
                )
                turn_has_discovery = True
                continue

            advisor_timeout = ctx.service._settings.composer_advisor_timeout_seconds
            effective_advisor_timeout = min(advisor_timeout, remaining)
            advisor_deadline_limited = remaining <= advisor_timeout

            # F2: consume budget BEFORE the outbound call, not after.
            # The cost guard's purpose is to bound outbound LiteLLM
            # calls regardless of outcome. Counting only successes
            # would let a flaky provider rack up unlimited failed
            # outbound calls until anti-anchor or the discovery-
            # turn limit fired. ARG_ERROR (above) and pre-call compose
            # timeout (above) do NOT consume advisor budget because no
            # outbound advisor call is made.
            advisor_calls_used += 1

            try:
                guidance, advisor_meta = await ctx.service._call_advisor_with_audit(
                    arguments,
                    recorder=recorder,
                    timeout=effective_advisor_timeout,
                )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                # Lifecycle exceptions: do not absorb. Propagate so
                # cancellation and shutdown work normally; the inner
                # ComposerLLMCall record was already fired by the
                # advisor method's finally block, so the audit trail
                # captures the cancelled call regardless.
                raise
            except TimeoutError as advisor_exc:
                if advisor_deadline_limited:
                    timeout_payload = {
                        "status": "COMPOSE_TIMEOUT",
                        "error": "Advisor call exceeded the remaining compose deadline.",
                        "error_class": "TimeoutError",
                        "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=timeout_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=timeout_payload,
                        error_class=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    advisor_compose_timeout = "in_flight"
                    break
                # Advisor-specific timeout with compose budget still
                # remaining: return structured tool feedback so the
                # composer can continue within its global deadline.
                advisor_error_payload = {
                    "status": "ADVISOR_ERROR",
                    "error": "Advisor call failed; no guidance returned.",
                    "error_class": type(advisor_exc).__name__,
                    "budget_used": advisor_calls_used,
                    "budget_remaining": budget - advisor_calls_used,
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=advisor_error_payload,
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=advisor_error_payload,
                    error_class=None,
                    error_message=None,
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(advisor_error_payload),
                    }
                )
                turn_has_discovery = True
                continue
            except Exception as advisor_exc:
                # Tier 3 boundary: outbound LLM call failed. Convert
                # to a structured tool-result error so the composer
                # LLM gets feedback rather than a silent stall. The
                # inner ComposerLLMCall record was already fired by
                # _call_advisor_with_audit's finally block, so the
                # audit trail captures the failure mode regardless.
                # Budget was already consumed above (F2) — the
                # outbound call attempt counts whether or not it
                # produced guidance.
                advisor_error_payload = {
                    "status": "ADVISOR_ERROR",
                    "error": "Advisor call failed; no guidance returned.",
                    "error_class": type(advisor_exc).__name__,
                    "budget_used": advisor_calls_used,
                    "budget_remaining": budget - advisor_calls_used,
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=advisor_error_payload,
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=advisor_error_payload,
                    error_class=None,
                    error_message=None,
                    post_version=state.version,
                )
                # Advisor failure IS counted for §7.7 anchor
                # tracking — repeated identical failed advisor
                # calls indicate the LLM is spamming a broken
                # prompt, exactly the pattern the tracker exists
                # to break.
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(advisor_error_payload),
                    }
                )
                turn_has_discovery = True
                continue

            success_payload = {
                "status": "SUCCESS",
                "guidance": guidance,
                "model": advisor_meta["model"],
                "prompt_tokens": advisor_meta["prompt_tokens"],
                "completion_tokens": advisor_meta["completion_tokens"],
                "cached_prompt_tokens": advisor_meta["cached_prompt_tokens"],
                "advisor_latency_ms": advisor_meta["latency_ms"],
                "budget_used": advisor_calls_used,
                "budget_remaining": budget - advisor_calls_used,
                "note": (
                    "ADVICE only — call the appropriate mutation tool to "
                    "apply any change. Do not echo this guidance back as "
                    "configuration without verifying it against the schema."
                ),
            }
            recorder.record(
                finish_success(
                    audit,
                    result_payload=success_payload,
                    version_after=state.version,
                )
            )
            _append_tool_outcome(
                response=success_payload,
                error_class=None,
                error_message=None,
                post_version=state.version,
            )
            # Successful advisor call is progress, not a failure —
            # the §7.7 tracker is reset for this tool name so a
            # subsequent set_pipeline failure does not inherit a
            # stale advisor anchor count.
            anti_anchor.record_success()
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(success_payload),
                }
            )
            continue

        # Session-aware async tool dispatch.
        #
        # ``_SESSION_AWARE_TOOL_HANDLERS`` holds handlers that AWAIT
        # session-service writers/readers; they cannot be dispatched
        # through the sync ``run_sync_in_worker(execute_tool, ...)``
        # path because execute_tool() is sync. The compose loop
        # intercepts these tool names BEFORE the generic dispatch
        # and awaits the handler directly, then routes the result
        # through the same audit envelope discipline as the sync
        # path (finish_success / finish_arg_error / finish_plugin_crash).
        #
        # Currently registered: ``request_interpretation_review``.
        # Adding another session-aware tool extends
        # ``_SESSION_AWARE_TOOL_HANDLERS`` and the per-tool
        # kwarg-build dict below; no new dispatch branch is needed.
        if is_session_aware_tool(tool_name):
            try:
                session_aware_outcome = await ctx.service._dispatch_session_aware_tool(
                    tool_name=tool_name,
                    tool_call_id=tool_call.id,
                    arguments=arguments,
                    state=state,
                    audit=audit,
                    recorder=recorder,
                    session_id=session_id,
                    current_state_id=current_state_id,
                    composer_model_version=provider_model_version,
                    llm_messages=llm_messages,
                    anti_anchor=anti_anchor,
                    policy_catalog=ctx.policy_catalog,
                )
            except (AssertionError, MemoryError, RecursionError, SystemError):
                # Same narrow-class discipline as the sync execute_tool
                # handler below: interpreter/Tier-1 invariant states must
                # unwind, never be laundered into a recoverable envelope.
                raise
            except AuditIntegrityError:
                # Tier-1 audit invariant — same passthrough as the sync path.
                raise
            except Exception as tool_exc:
                # elspeth-9c01c943a5: the session-aware dispatch converts
                # ToolArgumentError internally, so any exception reaching
                # here is a plugin/service bug. Without this capture it
                # escaped run_tool_batch and surfaced as an uncoded 500
                # with the accumulated turn state silently dropped. Route
                # it through the same plugin-crash carrier as the sync
                # path so the turn persists and the route layer answers
                # with the coded envelope.
                recorder.record(finish_plugin_crash(audit, exc=tool_exc))
                _append_tool_outcome(
                    response=None,
                    error_class=type(tool_exc).__name__,
                    error_message=type(tool_exc).__name__,
                    post_version=state.version,
                )
                plugin_crash = ComposerPluginCrashError.capture(
                    tool_exc,
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=recorder.invocations,
                    llm_calls=recorder.llm_calls,
                )
                plugin_crash_cause = tool_exc
                break
            all_cache_hits = False
            _append_tool_outcome(
                response=session_aware_outcome.result,
                error_class=session_aware_outcome.error_class,
                error_message=session_aware_outcome.error_message,
                post_version=session_aware_outcome.post_version,
            )
            if session_aware_outcome.is_discovery:
                turn_has_discovery = True
            else:
                turn_has_mutation = True
            if session_aware_outcome.result is not None:
                state = session_aware_outcome.result.updated_state
                last_validation = session_aware_outcome.result.validation
            continue

        # Precompute runtime preflight for preview_pipeline outside
        # the general side-effectful tool worker. This keeps
        # execute_tool() synchronous and bounds the async I/O cost
        # before it enters the worker thread pool.
        runtime_preflight_callback: RuntimePreflight | None = None
        if tool_name == "preview_pipeline":
            try:
                preview_preflight = await ctx.service._cached_runtime_preflight(
                    state,
                    user_id=user_id,
                    session_id=session_id,
                    cache=runtime_preflight_cache,
                    initial_version=initial_version,
                    session_scope=session_scope,
                    llm_calls=recorder.llm_calls,
                )
            except ComposerRuntimePreflightError as preflight_exc:
                recorder.record(finish_plugin_crash(audit, exc=preflight_exc.original_exc))
                raise ComposerRuntimePreflightError.capture(
                    preflight_exc.original_exc,
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=recorder.invocations,
                    llm_calls=recorder.llm_calls,
                ) from preflight_exc.original_exc

            def _make_preflight_callback(
                _result: ValidationResult = preview_preflight,
            ) -> RuntimePreflight:
                def _callback(_state: CompositionState) -> ValidationResult:
                    return _result

                return _callback

            runtime_preflight_callback = _make_preflight_callback()

        # All tool calls are offloaded to a worker to avoid blocking
        # the event loop.
        # Blob and secret tools perform synchronous filesystem
        # writes and SQLAlchemy transactions that would otherwise
        # stall the single-process web server for all concurrent
        # requests (rate-limit checks, websocket heartbeats,
        # progress broadcasts).
        #
        # Cancel-safety: tool calls are NOT wrapped in
        # asyncio.wait_for — they always run to completion.
        # The cooperative deadline is checked BETWEEN operations
        # (before LLM calls, after tool batches), so side effects
        # and state publication are never split.  LLM calls use
        # per-call wait_for because they are pure network I/O
        # with no side effects.
        #
        # Tool handlers raise ToolArgumentError at Tier-3 boundaries
        # (LLM supplied wrong types, semantically invalid values,
        # or malformed encodings that cannot be coerced).  The
        # compose loop catches ONLY that class and feeds the error
        # back to the LLM for retry.
        #
        # Any other exception — TypeError, ValueError, UnicodeError,
        # KeyError, AttributeError — escaping execute_tool() is a
        # plugin bug (Tier 1/2) and MUST crash.  Per CLAUDE.md,
        # silently laundering a plugin bug as an LLM-argument error
        # is worse than crashing: it pollutes the audit trail with
        # a confident but wrong Tier-3 story, and the LLM's "retry"
        # cannot correct a fault in our own code.
        # Dispatch under the structural audit envelope. The helper
        # records exactly one ComposerToolInvocation before this
        # await returns or raises, on every path:
        #   SUCCESS         → finish_success (with sentinel-canonical
        #                     fallback if canonical_json raises)
        #   ARG_ERROR       → finish_arg_error (caller's except block
        #                     below builds the LLM message)
        #   PLUGIN_CRASH (narrow re-raise) → finish_plugin_crash, then
        #                     re-raised so the outer compose loop exits
        #   PLUGIN_CRASH (general) → finish_plugin_crash, then the
        #                     caller's except block below wraps with
        #                     ComposerPluginCrashError.capture()
        #
        # Closes panel-review blockers B1 (canonical_json failure on
        # success path silently bypassed audit) and B2 (narrow
        # re-raise exited the loop unrecorded).
        # Bind loop-locals as default arguments so the closures
        # capture this iteration's values. The compose loop
        # awaits the helper synchronously inside the same
        # iteration, so late binding would not actually misfire
        # — but `B023 do not let function definitions reference
        # late-bound loop variables` is the project's structural
        # safeguard against future refactors that batch
        # iterations or introduce concurrency. Same pattern the
        # adjacent `_make_preflight_callback` already uses for
        # ``preview_preflight``.
        async def _do_dispatch(
            _tool_name: str = tool_name,
            _arguments: dict[str, Any] = arguments,
            _state: CompositionState = state,
            _last_validation: ValidationSummary | None = last_validation,
            _runtime_preflight_callback: RuntimePreflight | None = runtime_preflight_callback,
            _user_message_id: str | None = user_message_id,
            _user_message_content: str | None = user_message_content,
            _composer_model_identifier: str = ctx.service._model,
            _composer_model_version: str = provider_model_version,
            _composer_provider: str = ctx.service._availability.provider or "unknown",
            _composer_skill_hash: str = ctx.service._composer_skill_hash,
            _tool_arguments_hash: str = audit.binding_arguments_hash,
            _interpretation_requirements_are_internal: bool = interpretation_requirements_are_internal,
            _prevalidated_unapplied_result: ToolResult | None = prevalidated_unapplied_result,
            _preproposal_exception: BaseException | None = preproposal_exception,
        ) -> Any:
            if _preproposal_exception is not None:
                raise _preproposal_exception
            if _prevalidated_unapplied_result is not None:
                return _prevalidated_unapplied_result
            dispatched_result = await run_sync_in_worker(
                execute_tool,
                _tool_name,
                _arguments,
                _state,
                ctx.policy_catalog,
                plugin_snapshot=ctx.plugin_snapshot,
                data_dir=ctx.service._data_dir,
                session_engine=ctx.service._session_engine,
                session_id=session_id,
                secret_service=ctx.service._secret_service,
                user_id=user_id,
                prior_validation=_last_validation,
                runtime_preflight=_runtime_preflight_callback,
                max_blob_storage_per_session_bytes=ctx.service._settings.max_blob_storage_per_session_bytes,
                user_message_id=_user_message_id,
                user_message_content=_user_message_content,
                composer_model_identifier=_composer_model_identifier,
                composer_model_version=_composer_model_version,
                composer_provider=_composer_provider,
                composer_skill_hash=_composer_skill_hash,
                tool_arguments_hash=_tool_arguments_hash,
                validate_arguments=True,
                require_data_dir_for_paths=True,
                raise_schema_argument_errors=True,
                _interpretation_requirements_are_internal=_interpretation_requirements_are_internal,
            )
            if (
                _tool_name != "set_pipeline"
                and is_mutation_tool(_tool_name)
                and dispatched_result.success
                and dispatched_result.updated_state.version > _state.version
                and dispatched_result.validation.is_valid
            ):
                dispatched_result = await _finalize_completed_incremental_mutation(
                    dispatched_result,
                    _state,
                    plugin_snapshot=ctx.plugin_snapshot,
                    policy_catalog=ctx.policy_catalog,
                )
            return dispatched_result

        # ``_arg_error_payload`` is a module-level helper (F2 — testable
        # without spinning up the full compose loop). The nested
        # factory below binds ``tool_name`` to match the dispatch
        # ``arg_error_payload_factory`` signature. Default-arg binding
        # captures the loop-local ``tool_name`` at definition time.
        def _arg_error_payload_factory(
            _exc: ToolArgumentError,
            _tool_name: str = tool_name,
        ) -> Mapping[str, Any]:
            return _arg_error_payload(_exc, _tool_name)

        def _version_after(
            _result: Any,
            _prevalidated_unapplied_result: ToolResult | None = prevalidated_unapplied_result,
            _state: CompositionState = state,
        ) -> int:
            # The handler's ToolResult carries the new state on
            # ``updated_state``. Read here so the helper records
            # the post-mutation version inside the recorder call.
            if _prevalidated_unapplied_result is not None:
                return _state.version
            return cast(int, _result.updated_state.version)

        try:
            outcome = await dispatch_with_audit(
                recorder=recorder,
                audit=audit,
                do_dispatch=_do_dispatch,
                version_after_provider=_version_after,
                arg_error_payload_factory=_arg_error_payload_factory,
            )
        except ToolArgumentError as exc:
            # The audit record was already written by the helper.
            # Build the LLM-facing tool message and continue.
            if is_discovery_tool(tool_name):
                turn_has_discovery = True
            else:
                turn_has_mutation = True
            # Trust-boundary redaction: the echoed message reaches the
            # LLM API and (via audit) the Landscape. ToolArgumentError
            # is structurally safe by construction — the keyword-only
            # constructor accepts (argument, expected, actual_type)
            # and composes args[0] from those fields alone, so the
            # message cannot carry a raw LLM-supplied value. Belt-
            # and-suspenders: read ``exc.args[0]`` rather than
            # ``str(exc)`` so a future subclass that overrides
            # ``__str__`` to embed ``__cause__`` context (which may
            # carry DB URLs, filesystem paths, or secret fragments
            # from deeper layers) cannot leak through this path.
            # Handlers that use
            # ``raise ToolArgumentError(...) from exc`` get the
            # cause preserved on ``__cause__`` for debug/audit but
            # NOT echoed to the LLM.
            await emit_progress(
                progress,
                ComposerProgressEvent(
                    phase="using_tools",
                    headline="A tool request needed correction.",
                    evidence=("The tool rejected the request shape without exposing raw values.",),
                    likely_next="ELSPETH will ask the model to adjust the visible tool request.",
                ),
            )
            arg_error_payload = _arg_error_payload(exc, tool_name)
            _append_tool_outcome(
                response=None,
                error_class="ToolArgumentError",
                error_message=str(exc.args[0] if exc.args else "ToolArgumentError"),
                post_version=state.version,
            )
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(arg_error_payload),
                }
            )
            continue
        except (AssertionError, MemoryError, RecursionError, SystemError):
            # CLAUDE.md policy exception — DOCUMENTED DIVERGENCE.
            #
            # CLAUDE.md "Plugin Ownership" says a defective plugin
            # MUST crash rather than be wrapped and laundered as a
            # recoverable error.  The web server relaxes this for
            # ordinary exception classes (see the wider except
            # Exception below) because crashing the whole ASGI
            # process on one bad request would take down every
            # other concurrent session.
            #
            # The exceptions listed on this handler are NOT
            # relaxed: they represent states where the interpreter
            # or our own Tier-1 invariants are compromised and any
            # subsequent work — including the partial-state
            # persistence inside ``ComposerPluginCrashError.capture``
            # — would be operating on potentially-poisoned memory
            # or data.
            #
            # - AssertionError: a plain ``assert`` fired inside
            #   plugin code.  Asserts encode Tier-1 invariants
            #   (CLAUDE.md: "crash on any anomaly").  Writing the
            #   composition_states row after an invariant failure
            #   would persist data the invariant said was
            #   impossible.
            # - MemoryError / RecursionError: interpreter-level
            #   resource exhaustion.  The subsequent DB write may
            #   itself fail or corrupt state; better to unwind.
            # - SystemError: CPython internal invariant breach.
            #
            # ``BaseException``-only classes (SystemExit,
            # KeyboardInterrupt, GeneratorExit) already propagate
            # through ``except Exception`` below without any
            # handling here.
            #
            # Audit-record-then-raise discipline: ``dispatch_with_audit``
            # writes a ``PLUGIN_CRASH`` invocation BEFORE the
            # narrow-class exception leaves the helper. Building the
            # invocation reads only pre-captured scalars from the
            # frozen :class:`DispatchAudit` envelope plus
            # ``type(exc).__name__`` — no poisoned memory work.
            # Closes blocker B2 from the panel review (2026-05-04).
            raise
        except AuditIntegrityError:
            # Tier-1 audit invariant. Do not let the plugin-bug
            # catch-all below launder it into ComposerPluginCrashError.
            raise
        except Exception as tool_exc:
            # Plugin-bug path: any exception class OTHER than
            # ToolArgumentError escaping execute_tool() is a plugin
            # bug (CLAUDE.md tier 1/2). Capture the loop-local
            # ``state`` — which has been rebound to
            # result.updated_state on every successful prior
            # iteration — so the route layer can persist the
            # accumulated mutations into composition_states before
            # returning the 500. Without this, any tool call that
            # successfully mutated state prior to the crash would
            # be silently dropped from the state history.
            #
            # Web-server policy exception: CLAUDE.md says a
            # defective plugin must crash.  In the pipeline engine
            # (single-shot CLI process) that is straightforward —
            # abort the run.  In the web server a single malformed
            # request reaching a buggy tool handler would take the
            # ASGI worker down and abort every other concurrent
            # session, including audit writes, websocket progress
            # streams, and unrelated users.  We wrap the exception
            # into a typed ComposerPluginCrashError that surfaces
            # to the operator as an HTTP 500 with
            # ``type(exc).__name__`` in the structured log, and
            # preserves the original on ``__cause__`` for the ASGI
            # error machinery.  The handler directly above
            # re-raises the narrow set of exception classes that
            # MUST NOT be laundered, so the concession below is
            # bounded.
            #
            # Wrap narrow-scope: only exceptions from the
            # execute_tool call are wrapped here. Bugs in
            # _call_llm_before_deadline / _build_messages surface
            # through their own exception classes
            # (ComposerServiceError, ComposerConvergenceError).
            # Record PLUGIN_CRASH BEFORE raising — done structurally
            # by ``dispatch_with_audit``. The ``capture()`` helper
            # takes the recorder buffer (including the helper's
            # final crash record) so the route handler's
            # ``_handle_plugin_crash`` gets the complete sequence.
            _append_tool_outcome(
                response=None,
                error_class=type(tool_exc).__name__,
                error_message=type(tool_exc).__name__,
                post_version=state.version,
            )
            plugin_crash = ComposerPluginCrashError.capture(
                tool_exc,
                state=state,
                initial_version=initial_version,
                tool_invocations=recorder.invocations,
                llm_calls=recorder.llm_calls,
            )
            plugin_crash_cause = tool_exc
            break

        # SUCCESS path — the helper already recorded
        # ComposerToolStatus.SUCCESS via finish_success (with the
        # sentinel-canonical fallback for non-finite floats /
        # non-serializable result types). Update loop-local state
        # from outcome.result and continue with the LLM-message
        # append.
        version_before_tool = state.version
        result = outcome.result
        if prevalidated_unapplied_result is None:
            state = result.updated_state
            last_validation = result.validation
            last_runtime_preflight = result.runtime_preflight or last_runtime_preflight
        # Mark the (plugin_type, plugin_name) pair as
        # schema-loaded for this session when a get_plugin_schema
        # call returned a discovery success. The per-turn system
        # context renders this set as
        # ``composer_progress.schemas_loaded_this_session`` so
        # the model can compute its own schemas_gap without
        # re-introspecting plugins it has already seen. See
        # ``ComposerServiceImpl._mark_plugin_schema_loaded`` and
        # ``prompts.build_context_string``.
        #
        # Lifted from the prior inline compose-loop body into the
        # extracted ``_dispatch_tool_batch`` helper as part of the
        # parallel decomposition (commit f918f4269). The RC5.2
        # commit that introduced this tracker explicitly anticipated
        # the lift in its message: "the single line that will need
        # lift-and-shift into ``dispatch_tools`` when the parallel
        # ``_compose_loop`` decomposition lands".
        if tool_name == "get_plugin_schema" and result.success:
            ctx.service._mark_plugin_schema_loaded(
                session_id,
                str(arguments["plugin_type"]),
                str(arguments["name"]),
            )
        # §7.7 anchor tracking. ``finish_success`` records the audit
        # invocation regardless of ``result.success`` — the dispatch
        # itself ran without raising. But for anchor purposes we look
        # at ToolResult.success: a set_pipeline that returned a
        # validation-rejected state is the dominant anchor pattern
        # observed in the Tier 1 RED, and indistinguishable from a
        # ToolArgumentError as far as the LLM's retry loop is concerned.
        #
        # Successes only break the anchor when they are MUTATION
        # successes. Discovery successes (get_plugin_schema, list_*,
        # get_pipeline_state) are observations — empirically the model
        # interleaves them between failed mutation retries (see the
        # smoke session 55895523-... where 4 set_pipeline failures
        # were broken up by 1 get_pipeline_state and 1 get_plugin_schema,
        # both successful, both irrelevant to whether the model has
        # progressed on the anchored mutation).
        #
        # Mutation means a CompositionState version advance here.
        # Blob-store side effects such as create_blob/update_blob are
        # useful work, but they do not make an empty pipeline exist.
        if prevalidated_unapplied_result is not None:
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
        elif result.success:
            if _tool_result_mutated_composition_state(version_before=version_before_tool, result=result):
                mutation_success_observed = True
                anti_anchor.record_success()
        else:
            anti_anchor.record_failure(tool_name, audit.arguments_hash)
        result_json = _serialize_tool_result(result)
        await emit_progress(progress, tool_completed_progress_event(tool_name, result.success))
        _append_tool_outcome(
            response=result,
            error_class=None,
            error_message=None,
            post_version=state.version,
        )

        # Cache cacheable discovery results
        if is_cacheable_discovery_tool(tool_name):
            cache_key = _make_cache_key(tool_name, arguments)
            discovery_cache[cache_key] = _cached_discovery_payload(result)

        llm_messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_json,
            }
        )

        if not is_discovery_tool(tool_name):
            turn_has_mutation = True
        else:
            turn_has_discovery = True

    dispatch = _DispatchOutcome(
        state=state,
        last_validation=last_validation,
        last_runtime_preflight=last_runtime_preflight,
        tool_outcomes=tuple(tool_outcomes),
        decoded_args_by_call_id=decoded_args_by_call_id,
        turn_has_mutation=turn_has_mutation,
        turn_has_discovery=turn_has_discovery,
        all_cache_hits=all_cache_hits,
        plugin_crash=plugin_crash,
        plugin_crash_cause=plugin_crash_cause,
        advisor_compose_timeout=advisor_compose_timeout,
        assistant_message=assistant_message,
        raw_assistant_content=raw_assistant_content,
        assistant_tool_calls=assistant_tool_calls,
        mutation_success_observed=mutation_success_observed,
        pre_state_id=pre_state_id,
    )
    return dispatch, advisor_calls_used


_MIN_USEFUL_ADVISOR_SECONDS: Final[float] = 5.0


def _remaining_compose_seconds(deadline: float) -> float:
    """Return the compose budget available at the advisor boundary."""

    return deadline - asyncio.get_event_loop().time()
