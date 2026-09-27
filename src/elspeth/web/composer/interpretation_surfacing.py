"""Interpretation review evidence, draft preparation, and durable publication.

Layer: L3 (application). The owner receives session authority and fixed composer
provenance at construction; state and operation fences remain per call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from elspeth.contracts.composer_interpretation import InterpretationKind, InterpretationSource, InterpretationSurfaceOrigin
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.source_demand import (
    build_source_data_contract_draft,
    parse_source_data_contract_accepted_fields,
    sample_header_for_source,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools.sessions import interpretation_rate_cap_hit
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.interpretation_state import (
    BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX,
    INTERPRETATION_REQUIREMENTS_KEY,
    SOURCE_AUTHORING_KEY,
    InterpretationReviewSite,
    current_source_data_contract_demand,
    interpretation_sites,
    prompt_review_draft_from_options,
    source_name_from_component_id,
    vague_term_wiring_count,
    validate_pipeline_decision_node_semantics,
)

if TYPE_CHECKING:
    from elspeth.web.sessions.protocol import PreparedInterpretationEventDraft, SessionServiceProtocol


def _has_pending_prompt_template_requirement(options: Mapping[str, Any], *, user_term: str) -> bool:
    """Return True iff ``options`` carries a pending PT requirement for ``user_term``.

    Mirrors the precondition ``create_pending_interpretation_event`` enforces
    for ``llm_prompt_template`` (a single pending requirement matching the
    user_term). Reading the requirements directly keeps the backend-surface
    helper aligned with that writer-boundary gate.
    """

    if INTERPRETATION_REQUIREMENTS_KEY not in options:
        return False
    raw = options[INTERPRETATION_REQUIREMENTS_KEY]
    # NodeSpec freezes nested lists into tuples; pre-freeze test fixtures may
    # still exercise the list form. Any other present shape is internal drift.
    if type(raw) not in (list, tuple):
        raise InvariantError("_has_pending_prompt_template_requirement: interpretation requirements must be a list or tuple")
    matches = 0
    for requirement in raw:
        if type(requirement) not in (dict, MappingProxyType):
            raise InvariantError("_has_pending_prompt_template_requirement: interpretation requirement entries must be dict-shaped")
        requirement_map = cast(Mapping[str, Any], requirement)
        requirement_kind = requirement_map["kind"] if "kind" in requirement_map else InterpretationKind.VAGUE_TERM.value
        if requirement_kind != InterpretationKind.LLM_PROMPT_TEMPLATE.value:
            continue
        if requirement_map["status"] != "pending":
            continue
        requirement_term = requirement_map["user_term"]
        if type(requirement_term) is not str:
            raise InvariantError("_has_pending_prompt_template_requirement: prompt-template requirement user_term must be a string")
        if requirement_term.strip() == user_term.strip():
            matches += 1
    # (Task 7 LOW-a) Mirror _matching_pending_requirement_index's EXACTLY-ONE
    # multiplicity: create_pending raises on 0 or >1 matching pending PT
    # requirements. Return True only on exactly one so a duplicate-requirement
    # node is skipped to the fail-closed orphan gate, never crashed into an
    # opaque 500 at the writer boundary.
    return matches == 1


@observation_boundary(
    tier=3,
    source="web-authored node/source options mapping (untrusted interpretation requirements)",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant=(
        "returns the draft only when exactly one pending requirement matches "
        "(kind, user_term); any missing, mistyped, or ambiguous requirement data "
        "yields None and never raises"
    ),
)
def _matching_requirement_draft(
    options: Mapping[str, Any],
    *,
    kind: InterpretationKind,
    user_term: str,
) -> str | None:
    """Return the ``draft`` of the single pending requirement matching
    ``(kind, user_term)``, or ``None`` when there is not exactly one."""

    raw = options.get(INTERPRETATION_REQUIREMENTS_KEY)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    matches: list[str] = []
    for requirement in raw:
        if not isinstance(requirement, Mapping):
            continue
        requirement_kind = requirement.get("kind", InterpretationKind.VAGUE_TERM.value)
        if requirement_kind != kind.value:
            continue
        if requirement.get("status") != "pending":
            continue
        requirement_term = requirement.get("user_term")
        if not isinstance(requirement_term, str) or requirement_term.strip() != user_term.strip():
            continue
        draft = requirement.get("draft")
        if isinstance(draft, str) and draft:
            matches.append(draft)
    return matches[0] if len(matches) == 1 else None


def _resolvable_vague_term_count(
    state: CompositionState,
    *,
    node_id: str,
    term: str,
) -> int:
    """Count *resolvable* vague_term wirings for ``term`` on LLM node ``node_id``.

    Delegates to :func:`vague_term_wiring_count` so the repair loop's
    resolvability test cannot drift from the tool-boundary gate or the resolver
    contract. A pending requirement counts only when its substitution wiring (a
    ``prompt_template_parts`` ``interpretation_ref`` or a legacy
    ``{{interpretation:<term>}}`` placeholder) is present — which is what lets
    the loop catch a requirement whose wiring was stripped by a later mutation
    *after* its review event already existed (drift the tool boundary cannot
    re-check once the event is persisted).
    """
    for node in state.nodes:
        if node.id != node_id or node.plugin != "llm":
            continue
        return vague_term_wiring_count(node.options, user_term=term)
    return 0


async def _surfaced_evidence_keys(
    sessions_service: SessionServiceProtocol,
    *,
    session_id: str,
    current_state_id: str,
) -> frozenset[tuple[str, str, InterpretationKind]]:
    """Per-site surfacing evidence on one state, in ANY resolution status.

    Current-version interpretation_events rows bound to a committed state ARE
    the durable completion record for that state's surfacing debt: resolving
    or abandoning a review updates its row, it never removes it, and the state
    the rows bind to is immutable. A pending-only check would therefore read
    an already-resolved site as still owed and recreate it against stale
    historical state — which the writer boundary rejects outright once the
    placeholder has been consumed.
    """

    events = await sessions_service.list_interpretation_events(
        UUID(session_id),
        status="all",
        composition_state_id=UUID(current_state_id),
    )
    evidence: set[tuple[str, str, InterpretationKind]] = set()
    for event in events:
        if event.affected_node_id is None or event.user_term is None or event.kind is None:
            continue
        if event.kind is InterpretationKind.SOURCE_DATA_CONTRACT:
            if event.llm_draft is None:
                raise AuditIntegrityError("surfaced source data contract evidence requires its canonical draft")
            parse_source_data_contract_accepted_fields(event.llm_draft)
        evidence.add((event.affected_node_id, event.user_term, event.kind))
    return frozenset(evidence)


async def _auto_surface_prompt_template_reviews_for_state(
    state: CompositionState,
    *,
    sessions_service: SessionServiceProtocol,
    session_id: str,
    current_state_id: str,
    surface_origin: InterpretationSurfaceOrigin,
    model_identifier: str | None,
    model_version: str | None,
    provider: str | None,
    composer_skill_hash: str | None,
    session_operation_context: SessionOperationContext,
    already_surfaced: frozenset[tuple[str, str, InterpretationKind]] = frozenset(),
    repair_mode: bool = False,
) -> None:
    """Canonical ``llm_prompt_template`` surfacing pass (see the instance method).

    Module-level so persistence-layer callers that hold a sessions service but
    no composer instance (the guided wire-confirm settlement) can run the SAME
    surfacing the chat dispatcher and freeform settlement run, passing the
    provenance they actually hold (for guided commits: the proposal row's
    planner identity).
    """

    from elspeth.web.sessions.protocol import InterpretationResolveError

    for site in interpretation_sites(state):
        if site.kind is not InterpretationKind.LLM_PROMPT_TEMPLATE:
            continue
        surfaced = _backend_surface_args_for_site(state, site)
        if surfaced is None:
            continue
        affected_node_id, user_term, review_draft = surfaced
        if (affected_node_id, user_term, InterpretationKind.LLM_PROMPT_TEMPLATE) in already_surfaced:
            continue
        # The transactional writer owns kind-specific reviewed-content
        # identity. Calling it for every candidate preserves idempotence across
        # unrelated state versions while allowing same-text skeleton changes to
        # supersede stale cards.
        try:
            await sessions_service.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=UUID(current_state_id),
                affected_node_id=affected_node_id,
                tool_call_id=f"{BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}{uuid4()}",  # (D1)
                user_term=user_term,
                kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
                llm_draft=review_draft,
                session_operation_context=session_operation_context,
                surface_origin=surface_origin,
                model_identifier=model_identifier,  # (D2)
                model_version=model_version,  # (D2)
                provider=provider,  # (D2)
                composer_skill_hash=composer_skill_hash,  # (D2)
            )
        except InterpretationResolveError:
            # Settlement keeps the unguarded raise: a fresh state that cannot
            # accept its own surfacing IS a Tier-1 anomaly.
            if not repair_mode:
                raise
            # Repair cannot: the evidence read above is NOT atomic with this
            # write, and the site can be superseded in between — the node
            # removed or mutated by a later state, or the placeholder consumed
            # by a concurrent resolve. The writer boundary is the authority on
            # whether the debt still exists, and it has just said no. Skipping
            # keeps the already-verified stored response intact; raising would
            # turn a valid replay into a 500 over debt that no longer exists.
            continue


def _backend_surface_args_for_site(
    state: CompositionState,
    site: InterpretationReviewSite,
) -> tuple[str, str, str] | None:
    """Return ``(affected_node_id, user_term, llm_draft)`` for a site, or
    ``None`` when the writer-boundary precondition does not hold.

    Reads the draft straight from the node/source pending requirement so
    the strict ``create_pending_interpretation_event`` writer boundary
    accepts the insert. ``None`` means "no matching pending requirement" —
    the site is left for the run-time gate (designed advisory polarity).
    """

    if site.kind is InterpretationKind.INVENTED_SOURCE:
        source_name = source_name_from_component_id(site.component_id)
        if source_name is None:
            return None
        source = state.sources[source_name] if source_name in state.sources else None
        if source is None:
            return None
        options = source.options
        if SOURCE_AUTHORING_KEY not in options:
            return None
        draft = _matching_requirement_draft(options, kind=site.kind, user_term=site.user_term)
        if draft is None:
            return None
        return (site.component_id, site.user_term, draft)

    if site.kind is InterpretationKind.SOURCE_DATA_CONTRACT:
        # The data-contract card carries no staged requirement draft — the
        # draft is SERVER-COMPUTED from the graph's demand backtrace plus the
        # source's illustrative sample header, exactly as the
        # request_interpretation_review arm computes it
        # (tools/sessions.py::_assert_affected_component). The writer boundary
        # recomputes the same facts from the persisted head under the session
        # lock and rejects any divergence, so this surfacer can never persist
        # a stale or forged field list (elspeth-da68332faf work item 2).
        source_name = source_name_from_component_id(site.component_id)
        if source_name is None:
            return None
        source = state.sources[source_name] if source_name in state.sources else None
        if source is None or SOURCE_AUTHORING_KEY in source.options:
            return None
        demand = current_source_data_contract_demand(state, source_name)
        if not demand:
            return None
        draft = build_source_data_contract_draft(demand, sample_header_for_source(source))
        return (site.component_id, site.user_term, draft)

    node = next((candidate for candidate in state.nodes if candidate.id == site.component_id), None)
    if node is None:
        return None
    options = node.options
    # Prompt/model/vague cards share the event writer's LLM-transform
    # discriminator.  Checking only the requirement draft is insufficient:
    # an aggregation carrying copied LLM options, or a transform with no
    # prompt, still enumerates a fail-closed site but the writer rejects it.
    is_llm_transform = node.node_type == "transform" and node.plugin == "llm"
    if site.kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
        if not is_llm_transform:
            return None
        review_draft = prompt_review_draft_from_options(options)
        if not review_draft:
            raise InvariantError(
                "_auto_surface_prompt_template_reviews: prompt-template interpretation site lost its non-empty prompt surface"
            )
        # The writer requires exactly one matching pending requirement. A
        # requirement-free legacy site cannot become a resolvable card.
        if not _has_pending_prompt_template_requirement(options, user_term=site.user_term):
            return None
        # The card shows what the review attests: a multi-query node's whole
        # prompt surface, any other node's prompt_template. One derivation,
        # shared with the auto-stager that drafted the requirement — returning
        # prompt_template here put the dead node-level template on every
        # multi-query card.
        return (node.id, site.user_term, review_draft)
    if site.kind is InterpretationKind.LLM_MODEL_CHOICE:
        if not is_llm_transform:
            return None
        model = options["model"] if "model" in options else None
        if type(model) is not str or not model:
            return None
        # W1 (writer-boundary necessary-but-not-sufficient): the writer's
        # model_choice else-branch routes through _find_llm_transform_node,
        # which ALSO requires a non-empty prompt_template
        # (sessions/service.py). The model_choice SITE emitter fires on
        # `model` alone, so a model-only node yields a site the writer would
        # REJECT with InterpretationResolveError(ValueError). Guard the
        # precondition here (mirroring the PT path's
        # _has_pending_prompt_template_requirement) and leave the site
        # fail-closed at the run-time gate — the designed advisory polarity.
        prompt_template = options["prompt_template"] if "prompt_template" in options else None
        if type(prompt_template) is not str or not prompt_template:
            return None
        draft = _matching_requirement_draft(options, kind=site.kind, user_term=site.user_term)
        if draft is None or draft != model:
            return None
        return (node.id, site.user_term, draft)
    if site.kind is InterpretationKind.PIPELINE_DECISION:
        draft = _matching_requirement_draft(options, kind=site.kind, user_term=site.user_term)
        if draft is None:
            return None
        try:
            validate_pipeline_decision_node_semantics(
                node=node,
                all_nodes=state.nodes,
                user_term=site.user_term,
                draft=draft,
                context="backend interpretation-review surfacer",
            )
        except ValueError:
            return None
        return (node.id, site.user_term, draft)
    if site.kind is InterpretationKind.VAGUE_TERM:
        # Only authored/staged vague-term requirements are surfaced.
        # Bare legacy placeholders carry no requirement and are left
        # fail-closed at the run-time gate; never invent a draft.
        if not is_llm_transform:
            return None
        prompt_template = options["prompt_template"] if "prompt_template" in options else None
        if type(prompt_template) is not str or not prompt_template:
            return None
        if vague_term_wiring_count(options, user_term=site.user_term) != 1:
            return None
        draft = _matching_requirement_draft(options, kind=site.kind, user_term=site.user_term)
        if draft is None:
            return None
        return (node.id, site.user_term, draft)


def unsurfaceable_pending_interpretation_review_sites(
    state: CompositionState,
) -> tuple[InterpretationReviewSite, ...]:
    """Return execution-blocking review sites the backend cannot eventize.

    This is the pure pre-persistence view of the same site-to-writer argument
    authority used by :func:`surface_pending_interpretation_reviews_for_state`.
    Paste/import routes use it to reject atomically instead of committing a
    state whose fail-closed execution debt has no consumable review card.
    """

    return tuple(site for site in interpretation_sites(state) if _backend_surface_args_for_site(state, site) is None)


def prepare_pending_interpretation_event_drafts_for_state(
    state: CompositionState,
    *,
    surface_origin: InterpretationSurfaceOrigin,
    model_identifier: str | None,
    model_version: str | None,
    provider: str | None,
    composer_skill_hash: str | None,
) -> tuple[PreparedInterpretationEventDraft, ...]:
    """Prepare the generic surfacer's event cohort for atomic settlement.

    Prompt-template cards retain the established first-pass ordering; every
    kind still delegates to ``_backend_surface_args_for_site``, the one pure
    site-to-writer projection shared with asynchronous repair surfacing.
    Callers must reject ``unsurfaceable_pending_interpretation_review_sites``
    before invoking this function.
    """
    from elspeth.web.sessions.protocol import PreparedInterpretationEventDraft

    sites = interpretation_sites(state)
    ordered_sites = (
        *(site for site in sites if site.kind is InterpretationKind.LLM_PROMPT_TEMPLATE),
        *(site for site in sites if site.kind is not InterpretationKind.LLM_PROMPT_TEMPLATE),
    )
    drafts: list[PreparedInterpretationEventDraft] = []
    for site in ordered_sites:
        surfaced = _backend_surface_args_for_site(state, site)
        if surfaced is None:
            raise InvariantError("atomic interpretation cohort contains an unsurfaceable pending site")
        affected_node_id, user_term, llm_draft = surfaced
        drafts.append(
            PreparedInterpretationEventDraft(
                event_id=uuid4(),
                affected_node_id=affected_node_id,
                tool_call_id=f"{BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}{uuid4()}",
                user_term=user_term,
                kind=site.kind,
                llm_draft=llm_draft,
                surface_origin=surface_origin,
                model_identifier=model_identifier,
                model_version=model_version,
                provider=provider,
                composer_skill_hash=composer_skill_hash,
            )
        )
    return tuple(drafts)


async def surface_pending_interpretation_reviews_for_state(
    state: CompositionState,
    *,
    sessions_service: SessionServiceProtocol,
    session_id: str | None,
    current_state_id: str | None,
    surface_origin: InterpretationSurfaceOrigin,
    model_identifier: str | None,
    model_version: str | None,
    provider: str | None,
    composer_skill_hash: str | None,
    session_operation_context: SessionOperationContext,
    only_missing_evidence: bool = False,
) -> None:
    """Kind-general pending-review surfacer over one persisted state (B1).

    ``only_missing_evidence`` repairs a surfacing DEBT rather than surfacing
    afresh: every site already carrying evidence on this state — in any
    resolution status — is left alone, and only genuinely missing sites are
    written. The guided replay arm needs it because it re-runs this pass over
    a historical committed state that may since have been reviewed and
    superseded. Settlement-time callers leave it False: their state is new,
    nothing can have evidence yet, and the writer's own draft-aware dedup
    must stay free to supersede stale cards.

    Canonical shared implementation behind
    :meth:`InterpretationSurfacing.surface_pending_interpretation_reviews` — see
    that method's docstring for the polarity/skip contract. Module-level so
    the guided wire-confirm settlement (which holds a sessions service and
    the proposal row's planner provenance, but no composer instance) can run
    the SAME pass the chat dispatcher and freeform settlement run; without it
    a guided commit whose nodes carry pending requirements produces no event
    rows, no Accept card ever renders, and /execute fails closed with
    ``UnresolvedInterpretationPlaceholderError`` (tutorial session e1332b5a).
    """

    if session_id is None or current_state_id is None:
        return
    already_surfaced: frozenset[tuple[str, str, InterpretationKind]] = frozenset()
    if only_missing_evidence:
        already_surfaced = await _surfaced_evidence_keys(
            sessions_service,
            session_id=session_id,
            current_state_id=current_state_id,
        )
    if not _has_unsurfaced_site(state, already_surfaced):
        return
    # Surfacing is a write: the interpretation writer admits only COMPOSE or
    # PROPOSAL authority. A caller holding a shareable BLOB_READ admission
    # (the validate route's repair pass) escalates to its own COMPOSE lease
    # for the write step only — the read lease stays what it is, and a
    # pass with nothing to repair (the common page load) never takes one.
    if session_operation_context.operation_kind is SessionOperationKind.BLOB_READ:
        write_lease = await SessionOperationLease.acquire(
            sessions_service.session_operation_authority,
            session_id=UUID(session_id),
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=sessions_service.session_operation_owner_instance_id,
            lease_seconds=sessions_service.session_operation_lease_seconds,
        )
        try:
            await _surface_pending_interpretation_reviews_under_writer(
                state,
                sessions_service=sessions_service,
                session_id=session_id,
                current_state_id=current_state_id,
                surface_origin=surface_origin,
                model_identifier=model_identifier,
                model_version=model_version,
                provider=provider,
                composer_skill_hash=composer_skill_hash,
                already_surfaced=already_surfaced,
                only_missing_evidence=only_missing_evidence,
                session_operation_context=write_lease.context,
            )
        finally:
            await write_lease.close()
        return
    await _surface_pending_interpretation_reviews_under_writer(
        state,
        sessions_service=sessions_service,
        session_id=session_id,
        current_state_id=current_state_id,
        surface_origin=surface_origin,
        model_identifier=model_identifier,
        model_version=model_version,
        provider=provider,
        composer_skill_hash=composer_skill_hash,
        already_surfaced=already_surfaced,
        only_missing_evidence=only_missing_evidence,
        session_operation_context=session_operation_context,
    )


def _has_unsurfaced_site(
    state: CompositionState,
    already_surfaced: frozenset[tuple[str, str, InterpretationKind]],
) -> bool:
    """Whether the surfacer would write at least once: one predicate for both arms."""
    for site in interpretation_sites(state):
        surfaced = _backend_surface_args_for_site(state, site)
        if surfaced is None:
            continue
        affected_node_id, user_term, _draft = surfaced
        if (affected_node_id, user_term, site.kind) not in already_surfaced:
            return True
    return False


async def _surface_pending_interpretation_reviews_under_writer(
    state: CompositionState,
    *,
    sessions_service: SessionServiceProtocol,
    session_id: str,
    current_state_id: str,
    surface_origin: InterpretationSurfaceOrigin,
    model_identifier: str | None,
    model_version: str | None,
    provider: str | None,
    composer_skill_hash: str | None,
    already_surfaced: frozenset[tuple[str, str, InterpretationKind]],
    only_missing_evidence: bool,
    session_operation_context: SessionOperationContext,
) -> None:
    """Both surfacing arms under one writer's authority."""
    from elspeth.web.sessions.protocol import InterpretationResolveError

    # llm_prompt_template is already handled by the existing surfacer,
    # which carries the exact draft-aware dedup the writer boundary needs.
    await _auto_surface_prompt_template_reviews_for_state(
        state,
        sessions_service=sessions_service,
        session_id=session_id,
        current_state_id=current_state_id,
        surface_origin=surface_origin,
        model_identifier=model_identifier,
        model_version=model_version,
        provider=provider,
        composer_skill_hash=composer_skill_hash,
        already_surfaced=already_surfaced,
        repair_mode=only_missing_evidence,
        session_operation_context=session_operation_context,
    )
    for site in interpretation_sites(state):
        if site.kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
            continue  # handled above
        surfaced = _backend_surface_args_for_site(state, site)
        if surfaced is None:
            continue
        affected_node_id, user_term, llm_draft = surfaced
        if (affected_node_id, user_term, site.kind) in already_surfaced:
            continue
        # Do not pre-deduplicate by draft text here. The writer compares the
        # canonical per-kind reviewed artifact under the session transaction,
        # reusing only coherent authority and abandoning superseded cards.
        # W1 backstop: the per-kind precondition above is NECESSARY but not
        # always SUFFICIENT (e.g. pipeline_decision must additionally pass
        # validate_pipeline_decision_semantics, which the surfacer does not
        # replicate). create_pending_interpretation_event raises
        # InterpretationResolveError on an expected boundary mismatch, and this runs AFTER
        # save_composition_state at a persist seam with NO outer except — so
        # an unguarded raise would 500 and wedge the session. Skip the site
        # instead; it stays fail-closed at the run-time gate (advisory
        # polarity). Deliberately not slog'd: a skipped advisory surface is
        # not a telemetry/audit event, matching the existing surfacer's
        # silent precondition skips.
        try:
            await sessions_service.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=UUID(current_state_id),
                affected_node_id=affected_node_id,
                tool_call_id=f"{BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX}{uuid4()}",
                user_term=user_term,
                kind=site.kind,
                llm_draft=llm_draft,
                session_operation_context=session_operation_context,
                surface_origin=surface_origin,
                model_identifier=model_identifier,
                model_version=model_version,
                provider=provider,
                composer_skill_hash=composer_skill_hash,
            )
        except InterpretationResolveError:
            continue


class InterpretationSurfacing:
    """Owns interpretation review reads, draft admission, and publication."""

    def __init__(
        self,
        *,
        sessions_service: SessionServiceProtocol | None,
        per_term_cap: int,
        per_session_day_cap: int,
        model_identifier: str,
        provider: str,
        composer_skill_hash: str,
    ) -> None:
        self._sessions_service = sessions_service
        self._per_term_cap = per_term_cap
        self._per_session_day_cap = per_session_day_cap
        self._model_identifier = model_identifier
        self._provider = provider
        self._composer_skill_hash = composer_skill_hash

    def _sessions_service_required(self) -> SessionServiceProtocol:
        if self._sessions_service is None:
            raise RuntimeError("sessions_service not wired")
        return self._sessions_service

    async def _rate_capped_vague_term_sites(
        self,
        sites: tuple[tuple[str, str, InterpretationKind], ...],
        *,
        session_id: str | None,
        current_state_id: str | None,
    ) -> frozenset[tuple[str, str, InterpretationKind]]:
        """Return the vague_term sites whose review request a rate cap refuses now.

        Answers in the handler's order for a request on the current persisted
        state: a site with a user-approved event for its exact
        ``(kind, user_term, affected_node_id)`` on this branch is answered by
        the dedup gate (idempotent or duplicate), never by a cap; every other
        site is checked with :func:`interpretation_rate_cap_hit`, the counting
        rule the handler refuses with. The ``AUTO_INTERPRETED_NO_SURFACES`` row
        a refusal writes carries no node or term, so the refusal itself cannot
        be matched to a site; recomputing the cap can.
        """
        candidates = tuple(site for site in sites if site[2] is InterpretationKind.VAGUE_TERM)
        if session_id is None or current_state_id is None or not candidates:
            return frozenset()
        sessions_service = self._sessions_service_required()
        events = await sessions_service.list_interpretation_events(UUID(session_id), status="all")
        composition_state_id = UUID(current_state_id)
        now = datetime.now(UTC)
        capped: set[tuple[str, str, InterpretationKind]] = set()
        for site in candidates:
            component_id, user_term, kind = site
            if any(
                event.interpretation_source is InterpretationSource.USER_APPROVED
                and event.composition_state_id == composition_state_id
                and event.kind is kind
                and event.user_term == user_term
                and event.affected_node_id == component_id
                for event in events
            ):
                continue
            cap_hit = interpretation_rate_cap_hit(
                events,
                user_term=user_term,
                composition_state_id=composition_state_id,
                per_term_cap=self._per_term_cap,
                per_session_day_cap=self._per_session_day_cap,
                now=now,
            )
            if cap_hit is not None:
                capped.add(site)
        return frozenset(capped)

    async def _missing_pending_interpretation_review_sites(
        self,
        state: CompositionState,
        *,
        session_id: str | None,
    ) -> tuple[tuple[str, str, InterpretationKind], ...]:
        """Return pending interpretation handoffs that cannot be resolved."""

        sites = interpretation_sites(state)
        if session_id is None:
            return ()
        sessions_service = self._sessions_service_required()
        events = await sessions_service.list_interpretation_events(UUID(session_id), status="pending")
        pending_sites = {
            (event.affected_node_id, event.user_term.strip(), event.kind)
            for event in events
            if event.affected_node_id is not None and event.user_term is not None and event.kind is not None
        }
        missing_or_unresolvable: dict[tuple[str, str, InterpretationKind], None] = {}
        for site in sites:
            site_key = (site.component_id, site.user_term, site.kind)
            if site_key not in pending_sites:
                missing_or_unresolvable[site_key] = None
        for event in events:
            if event.kind is not InterpretationKind.VAGUE_TERM or event.affected_node_id is None or event.user_term is None:
                continue
            event_site_key = (event.affected_node_id, event.user_term.strip(), event.kind)
            wiring_count = _resolvable_vague_term_count(
                state,
                node_id=event_site_key[0],
                term=event_site_key[1],
            )
            if wiring_count != 1:
                missing_or_unresolvable[event_site_key] = None
        return tuple(missing_or_unresolvable)

    async def _auto_surface_prompt_template_reviews(
        self,
        state: CompositionState,
        *,
        session_id: str | None,
        current_state_id: str | None,
        session_operation_context: SessionOperationContext | None = None,
    ) -> None:
        """Surface a pending ``llm_prompt_template`` review EVENT, backend-derived.

        For every LLM node that carries a pending auto-staged
        ``llm_prompt_template`` requirement and does not yet have a pending event
        for it, create the pending event against the FINAL frozen skeleton at
        turn finalization. Because the skeleton can no longer mutate this turn
        once we reach the orphan gate, a review surfaced here can never go stale
        against a later skeleton mutation (elspeth-e51216d305 Case B). Idempotent
        (skips nodes that already have a pending PT event) and a no-op when there
        is no session or no persisted state id. See (D1)-(D5) in the plan.

        The honest provenance sentinel ``tool_call_id="backend_auto_surface:..."``
        (D1) records that no LLM tool call produced this event; the user still
        reviews it, so ``interpretation_source`` stays ``user_approved``.
        """

        if session_id is None or current_state_id is None:
            return
        # (D2 / Task 7 LOW-b) model_version == model_identifier == self._model_identifier
        # is INTENTIONAL here: a backend-derived surface has no LLM response
        # object to resolve a provider-reported model from, so we cannot use
        # the LLM-surfaced path's safe_response_model(response). This deliberate
        # divergence is the most audit-honest value available at this surface.
        if session_operation_context is None:
            raise RuntimeError("pending interpretation surfacing requires the compose operation context")
        await _auto_surface_prompt_template_reviews_for_state(
            state,
            sessions_service=self._sessions_service_required(),
            session_id=session_id,
            current_state_id=current_state_id,
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier=self._model_identifier,  # (D2)
            model_version=self._model_identifier,  # (D2)
            provider=self._provider,  # (D2)
            composer_skill_hash=self._composer_skill_hash,  # (D2)
            session_operation_context=session_operation_context,
        )

    async def surface_pending_interpretation_reviews(
        self,
        state: CompositionState,
        *,
        session_id: str | None,
        current_state_id: str | None,
        only_missing_evidence: bool = False,
        session_operation_context: SessionOperationContext,
    ) -> None:
        """Kind-general backend surfacer for the GUIDED commit path (B1).

        The freeform fail-closed orphan gate
        (:meth:`_missing_pending_interpretation_review_sites`) is unreachable
        from the guided dispatcher, so guided commits that create
        interpretation sites would otherwise orphan and only fail at run
        time with ``UnresolvedInterpretationPlaceholderError``. This pass runs
        after every site-creating guided commit (source / transform /
        recipe-apply) and surfaces a resolvable pending EVENT for every site
        whose writer-boundary precondition holds — covering every
        ``InterpretationKind`` member, not just ``llm_prompt_template``.

        Each branch reads the site's ``draft``/``user_term`` from the node or
        source requirement so the strict per-kind writer boundary
        (``create_pending_interpretation_event``) accepts the insert; a site
        with no matching pending requirement (e.g. a bare legacy vague-term
        token) is SKIPPED and left fail-closed at the run-time gate, the
        designed advisory polarity (spec §5 B1). No backend word-list heuristic
        and no synthesized "cool"/"legacy" draft are permitted.

        Honest provenance: the sentinel ``tool_call_id="backend_auto_surface:..."``
        records that no LLM tool call produced the event; the user still
        reviews it, so ``interpretation_source`` stays ``user_approved``.
        Idempotent and a no-op when there is no session/persisted state.

        ``only_missing_evidence=True`` is the /validate backstop mode
        (elspeth-03f5728c33): a compose that dies after persisting its mutating
        turn (deferred cancellation, convergence timeout, plugin crash) never
        reaches the finalize surfacer, stranding pending requirements with no
        event rows. Repair mode surfaces only the genuinely missing sites and
        leaves every site already carrying evidence — in any resolution
        status — untouched, so re-running it over a partially reviewed state
        neither duplicates live cards nor resurrects resolved ones.
        """

        if session_id is None or current_state_id is None:
            return
        await surface_pending_interpretation_reviews_for_state(
            state,
            sessions_service=self._sessions_service_required(),
            session_id=session_id,
            current_state_id=current_state_id,
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier=self._model_identifier,
            model_version=self._model_identifier,
            provider=self._provider,
            composer_skill_hash=self._composer_skill_hash,
            only_missing_evidence=only_missing_evidence,
            session_operation_context=session_operation_context,
        )
