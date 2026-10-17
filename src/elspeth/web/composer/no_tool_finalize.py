"""No-tool-response finalizer for the composer compose loop.

Extracted from ComposerServiceImpl._finalize_no_tool_response. The runtime
validation dependency is the Composer preflight owner.

Validation notices preserve the provider's prose and its audit/history
identity. Semantic publication review belongs to the upstream END gate.
"""

from __future__ import annotations

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.discovery_cache import RuntimePreflightCache as _RuntimePreflightCache
from elspeth.web.composer.no_tool_policy import (
    blocking_result_from_tool_invocations as _blocking_result_from_tool_invocations,
)
from elspeth.web.composer.no_tool_policy import (
    carries_build_action as _carries_build_action,
)
from elspeth.web.composer.no_tool_policy import compose_clarification_message
from elspeth.web.composer.no_tool_policy import (
    compose_empty_state_message as _compose_empty_state_message,
)
from elspeth.web.composer.no_tool_policy import (
    compose_preflight_failure_message as _compose_preflight_failure_message,
)
from elspeth.web.composer.no_tool_policy import (
    enforce_augmentation_prefix_invariant as _enforce_augmentation_prefix_invariant,
)
from elspeth.web.composer.no_tool_policy import (
    is_pending_interpretation_handoff as _is_pending_interpretation_handoff,
)
from elspeth.web.composer.no_tool_policy import (
    last_mutation_was_pending_proposal as _last_mutation_was_pending_proposal,
)
from elspeth.web.composer.no_tool_policy import (
    no_mutation_empty_state_validation as _no_mutation_empty_state_validation,
)
from elspeth.web.composer.no_tool_policy import (
    state_is_structurally_empty as _state_is_structurally_empty,
)
from elspeth.web.composer.no_tool_response import NoToolResponse, is_unstarted_clarification, parse_no_tool_response
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


async def finalize_no_tool_response(
    preflight: ComposerPreflight,
    *,
    content: str,
    no_tool_response: NoToolResponse | None = None,
    state: CompositionState,
    initial_version: int,
    user_id: str | None,
    session_id: str | None,
    last_runtime_preflight: ValidationResult | None,
    runtime_preflight_cache: _RuntimePreflightCache,
    session_scope: str,
    user_message: str = "",
    mutation_success_seen: bool = False,
    tool_invocations: tuple[ComposerToolInvocation, ...] = (),
    llm_calls: tuple[ComposerLLMCall, ...] = (),
    plugin_snapshot: PluginAvailabilitySnapshot | None = None,
    session_operation_context: SessionOperationContext | None = None,
) -> ComposerResult:
    """Apply the deterministic final-gate check and build a ComposerResult.

    The validation augmentation shapes below preserve the provider's prose (the
    ``routes._composer_history_content`` discriminator depends on the
    ``content`` / ``raw_assistant_content`` relationship below):

    1. **No-mutation empty-state augmentation** — the user's message
       carried a build action (``carries_build_action``), no successful
       mutation has been seen this
       turn, and the state is structurally empty. The model's prose
       is passed through verbatim with an operator-facing suffix
       appended (concrete blocker if a tool failed). ``content``
       starts with ``raw_assistant_content`` so the LLM keeps seeing
       its own prose on subsequent turns.
    2. **Preflight-invalid empty-state augmentation** — preflight
       is invalid AND the state is structurally empty. Same
       augmentation shape as (1); ``raw_assistant_content`` carries
       the unaugmented prose so the LLM context is unaffected by the
       operator-facing suffix.
    3. **Preflight-invalid non-empty-state augmentation** —
       preflight is invalid and the state is non-empty. Panel-evals
       evidence (issue elspeth-9cfbad6901) showed the model's prose
       in this case is typically substantive disclosure rather than
       a false completion claim — the model names removed nodes,
       chosen operational semantics, and its own diagnosis. The
       prose is preserved verbatim with a system-attributed suffix
       naming the validator's specific objection appended;
       ``raw_assistant_content`` carries the unaugmented prose so
       the LLM-context channel sees the model's own prose on
       subsequent turns. Self-correction continues to land via the
       model's next ``preview_pipeline`` call.

    When preflight is valid or unknown, the response passes through unchanged
    unless the typed response requires an unstarted-clarification notice.
    This finalizer does not infer claims or contradictions from English prose.

    Gate logic (no regex on natural-language text governs the
    routing-shape decision):
    - If ``state.version > initial_version`` (state changed this turn),
      run ``_cached_runtime_preflight`` for the current state.
    - Otherwise, reuse ``last_runtime_preflight`` from the most recent
      ``preview_pipeline`` call (may be ``None``).
    - When that leaves the verdict unknown (``None``) on a WIRED state
      (sources and outputs present) whose own Stage-1 ``state.validate()``
      is invalid, run the preflight anyway (elspeth-ac85b0ab0e): a build
      damaged on a prior turn must not finalize as a bare pass-through.
      Half-built intermediates keep the bare passthrough.

    Args:
        content: The model's assistant prose for this turn.
        state: The post-tool composition state. ``state.version``
            versus ``initial_version`` decides whether preflight
            must re-run.
        initial_version: The composition version at turn start.
        user_id: Authenticated user identity for cache scoping.
        last_runtime_preflight: Most recent preflight outcome from
            ``preview_pipeline`` calls this turn; ``None`` if none.
        runtime_preflight_cache: Per-turn cache to avoid redundant
            preflight invocations on identical state.
        session_scope: Scope identifier for cache + telemetry.
        user_message: The user's message that triggered this turn.
            Used to detect "build-style" requests for the
            no-mutation empty-state augmentation path; a purely
            conversational message suppresses that path.
        mutation_success_seen: Whether any mutating tool call
            succeeded this turn. Suppresses the no-mutation
            augmentation path.
        tool_invocations: Tool calls made this turn; the most
            recent failed build/edit (non-discovery) call feeds the
            operator-facing blocker suffix.
        llm_calls: LLM call audit rows for this turn.

    Unexpected preflight exceptions (anything other than a
    ``RuntimePreflightFailure`` caught inside ``_cached_runtime_preflight``)
    are already converted to ``ComposerRuntimePreflightError`` with
    partial-state preservation by the coordinator's exception handling
    path — they are not caught here.
    """
    if no_tool_response is None:
        no_tool_response = parse_no_tool_response(content)
    content = no_tool_response.body
    unstarted_clarification = is_unstarted_clarification(
        no_tool_response,
        state=state,
        initial_version=initial_version,
        mutation_success_seen=mutation_success_seen,
        tool_invocations=tool_invocations,
    )
    if (
        _carries_build_action(user_message)
        and not mutation_success_seen
        and _state_is_structurally_empty(state)
        and not _last_mutation_was_pending_proposal(tool_invocations)
        and not unstarted_clarification
    ):
        # No-mutation empty-state augmentation. The model produced
        # honest diagnostic prose about what it tried and what blocked
        # convergence (audit-DB inspection across 2026-05-08 panel-cohort
        # cells confirms this). Pass the prose through and append a
        # system-attributed suffix carrying the concrete blocker — the
        # earlier full-replacement behavior hid the model's actual
        # output from both the user and (via routes._composer_history_content)
        # from the model itself on subsequent turns
        # (cf. elspeth-861b0c58f5).
        #
        # Disclosure is gated on _carries_build_action, NOT on the
        # mutation-intent enum (issue elspeth-a6a2ed6b1d). This branch
        # previously required EXPLICIT_MUTATION, which is a packaging
        # verdict — the grammar fullmatches the whole normalised message
        # from character 0 and treats a single "?" anywhere as a hard
        # disqualifier. Over the acceptance corpus that split
        # otherwise-identical requests: 14 of 18 phrasings classified
        # AMBIGUOUS and silently lost the explanation of why nothing got
        # built. Whether a user is owed that explanation cannot turn on
        # the article after the verb.
        #
        # Gating on "is not CONVERSATIONAL" instead was tried and
        # rejected on review: AMBIGUOUS is also reached by two early
        # returns that never test content (the input-size ceiling and
        # the unbalanced-quote guard), so an oversize or
        # unbalanced-quote greeting failed OPEN into a build-failure
        # notice. The named predicate always runs the content test, so
        # neither short-circuit decides disclosure. Routing is
        # unaffected: the surface-selection call sites read
        # classify_pipeline_mutation_intent, which this path no longer
        # consults.
        #
        # Exception: APPROVAL_REQUIRED proposals are not failures. Under
        # explicit_approve trust mode the build SUCCEEDED — the work is
        # queued for human approval and the state will advance on accept.
        # The model's prose ("approval pending state, not applied") is
        # already truthful; injecting "[ELSPETH-SYSTEM] pipeline is still
        # empty" over it corrupts both operator framing and (via the
        # synthesized suffix being re-read on subsequent turns) the
        # model's state model.
        blocker = _blocking_result_from_tool_invocations(tool_invocations)
        empty_state_runtime_result = _no_mutation_empty_state_validation(blocker)
        augmented_message = _compose_empty_state_message(content, blocker=blocker)
        _enforce_augmentation_prefix_invariant(
            branch="no_mutation_empty_state_augmentation",
            content=content,
            augmented=augmented_message,
        )
        return ComposerResult(
            message=augmented_message,
            state=state,
            runtime_preflight=empty_state_runtime_result,
            raw_assistant_content=content,
            tool_invocations=tool_invocations,
            llm_calls=llm_calls,
        )

    # The reuse/recompute rule AND the cross-turn arm (elspeth-ac85b0ab0e)
    # live in ``_reuse_or_recompute_runtime_preflight`` — the same method the
    # repair and advisor gates consume via ``_turn_runtime_preflight`` — so
    # this finalize observes the SAME verdict by construction. The one
    # divergence is deliberate: no structurally-empty guard here, because the
    # empty-state finalize branches above own that population and a MUTATED
    # empty state (e.g. a cleared source) must still pay the preflight so the
    # ``preflight_invalid_empty_state_augmentation`` branch below can carry
    # the real validator objection.
    runtime_result: ValidationResult | None = await preflight.reuse_or_recompute_runtime_preflight(
        state=state,
        user_id=user_id,
        session_id=session_id,
        last_runtime_preflight=last_runtime_preflight,
        runtime_preflight_cache=runtime_preflight_cache,
        initial_version=initial_version,
        session_scope=session_scope,
        llm_calls=llm_calls,
        plugin_snapshot=plugin_snapshot,
        session_operation_context=session_operation_context,
    )

    if runtime_result is not None and not runtime_result.is_valid and not _is_pending_interpretation_handoff(runtime_result):
        # Two finalize shapes for invalid preflight, dispatched on
        # state structure. Both augment — the difference is suffix
        # wording (issue elspeth-9cfbad6901 unified the policy after
        # the original replacement-on-non-empty-state branch was
        # found to discard substantive model disclosure):
        #
        # 1. Preflight-invalid empty-state augmentation: state is
        #    structurally empty. The model produced honest diagnostic
        #    prose about what it tried and what blocked convergence.
        #    Pass the prose through and append a system suffix asking
        #    the operator to refine or retry. raw_assistant_content
        #    carries the unaugmented prose so
        #    routes._composer_history_content replays it to the LLM
        #    on subsequent turns without the synthetic system text
        #    (cf. elspeth-861b0c58f5 — the original synthesizer-
        #    replaces-prose behavior corrupted both user view and
        #    LLM context).
        #
        # 2. Preflight-invalid non-empty-state augmentation: state
        #    has been populated AND preflight failed. Panel-evals
        #    evidence (fork_coalesce__p4_adversarial_engineer,
        #    boolean_routing__p3_marketingops; see issue
        #    elspeth-9cfbad6901) shows the model's prose in this
        #    case is typically substantive disclosure — what was
        #    attempted, removed nodes, chosen semantics — rather
        #    than a false completion claim. Pass the prose through
        #    and append a system suffix naming the validator's
        #    specific objection. The model's next preview_pipeline
        #    call surfaces the failure on the self-correction loop;
        #    the operator-facing suffix is stripped from LLM history
        #    so the LLM sees only its own prose.
        if _state_is_structurally_empty(state):
            augmented_message = _compose_empty_state_message(content)
            _enforce_augmentation_prefix_invariant(
                branch="preflight_invalid_empty_state_augmentation",
                content=content,
                augmented=augmented_message,
            )
            return ComposerResult(
                message=augmented_message,
                state=state,
                runtime_preflight=runtime_result,
                raw_assistant_content=content,
                tool_invocations=tool_invocations,
                llm_calls=llm_calls,
            )
        augmented_message = _compose_preflight_failure_message(content, runtime_result=runtime_result)
        _enforce_augmentation_prefix_invariant(
            branch="preflight_invalid_non_empty_state_augmentation",
            content=content,
            augmented=augmented_message,
        )
        return ComposerResult(
            message=augmented_message,
            state=state,
            runtime_preflight=runtime_result,
            raw_assistant_content=content,
            tool_invocations=tool_invocations,
            llm_calls=llm_calls,
        )

    if unstarted_clarification:
        augmented_message = compose_clarification_message(content)
        _enforce_augmentation_prefix_invariant(
            branch="clarification_augmentation",
            content=content,
            augmented=augmented_message,
        )
        return ComposerResult(
            message=augmented_message,
            state=state,
            runtime_preflight=runtime_result,
            raw_assistant_content=content,
            tool_invocations=tool_invocations,
            llm_calls=llm_calls,
        )

    return ComposerResult(
        message=content,
        state=state,
        runtime_preflight=runtime_result,
        tool_invocations=tool_invocations,
        llm_calls=llm_calls,
    )
