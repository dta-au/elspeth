"""Structured interpretation-review state for composer-authored LLM prompts.

Layer: L3 web application.

The runtime LLM plugin owns ``prompt_template`` as real Jinja prompt text.
Human-review workflow state is represented here as structured authoring
metadata on the web composition node and stripped before engine configuration.
Legacy ``{{interpretation:<term>}}`` prompts are still detected so older session
states can be opened and resolved during the migration window.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import get_close_matches
from typing import Any, Final, Literal, NotRequired, TypedDict

from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
from elspeth.contracts.trust_boundary import observation_boundary, trust_boundary
from elspeth.core.prompt_artifact import approved_prompt_artifact_hash
from elspeth.plugins.infrastructure.manager import untrusted_content_transform_names
from elspeth.web.composer.source_demand import (
    SOURCE_DATA_CONTRACT_USER_TERM,
    backtraced_source_demand,
    parse_source_data_contract_accepted_fields,
    source_data_contract_artifact_hash,
    source_data_contract_fields_for_demand_recompute,
)

# ``SOURCE_AUTHORING_KEY`` is re-exported in the explicit ``X as X`` form on
# purpose. Six composer modules (service, pipeline_proposal,
# reviewed_source_authority, tools/_common, tools/sources, tools/sessions)
# source this key from the interpretation-state facade rather than from
# composer.state; this module declares no ``__all__``, so under mypy's
# no_implicit_reexport the plain import form makes every one of those imports
# an attr-defined error (introduced by 3dc67fb1d).
from elspeth.web.composer.state import (
    SOURCE_AUTHORING_KEY as SOURCE_AUTHORING_KEY,
)
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    SourceSpec,
    _coalesce_branch_connections,
    _well_formed_query_entries,
)
from elspeth.web.plugin_policy.coverage import (
    OutputStreamGraph as _OutputStreamGraph,
)
from elspeth.web.plugin_policy.coverage import (
    build_output_stream_graph as _output_stream_graph,
)
from elspeth.web.plugin_policy.coverage import node_has_blocking_control, node_has_capability
from elspeth.web.validation import INTERPRETATION_PLACEHOLDER_RE

INTERPRETATION_REQUIREMENTS_KEY = "interpretation_requirements"
PROMPT_TEMPLATE_PARTS_KEY = "prompt_template_parts"
SOURCE_COMPONENT_ID = "source"
INTERPRETATION_REVIEW_PENDING_CODE = "interpretation_review_pending"
INTERPRETATION_REVIEW_DRIFT_CODE = "interpretation_review_drift"
PENDING_INTERPRETATION_AUTHORING_TEXT = "pending interpretation"
RAW_HTML_CLEANUP_USER_TERM: Final[str] = "drop_raw_html_fields"
# The CLOSED set of reviewable pipeline-decision terms. Every member must have
# a projection helper in ``pipeline_decision_artifact_hash`` — that registry
# raises on unknown terms at resolve time, so membership here is what makes an
# authored pipeline_decision requirement resolvable at all.
REGISTERED_PIPELINE_DECISION_USER_TERMS: Final[frozenset[str]] = frozenset(
    {
        "drop_raw_html_fields",
        "web_scrape_http_identity",
        "prompt_injection_shield_recommendation",
        "required_control_auto_wired",
        "gate_condition_authored",
    }
)
# Sink-neutral wording (pack pressure-suite run 2, G6): the old "JSON output"
# clause forced a false audit statement onto CSV/text sinks. The recognition
# markers ("raw html", "fingerprint") are unchanged.
RAW_HTML_CLEANUP_REVIEW_DRAFT: Final[str] = "Drop the scraped raw HTML and fingerprint fields before saving the output."
WEB_SCRAPE_HTTP_IDENTITY_USER_TERM: Final[str] = "web_scrape_http_identity"
PROMPT_SHIELD_USER_TERM: Final[str] = "prompt_injection_shield_recommendation"
# Acknowledgeable disclosure for a control node the server spliced into the
# graph because deployment policy makes that control REQUIRED (R2-F10,
# elspeth-f99655f540). The row rides on the INSERTED node, staged pending by
# ``web.composer.required_controls.wire_required_controls``.
REQUIRED_CONTROL_AUTO_WIRED_USER_TERM: Final[str] = "required_control_auto_wired"
# Planner-authored gate semantics (elspeth-c2c35e52ae). The composer prompt
# doctrine tells the planner to escalate a gate threshold, category literal, or
# route direction it CHOSE ITSELF rather than carrying the user's stated value
# verbatim. That instruction was inert until this term existed: an unregistered
# term is rejected at ``validate_pipeline_decision_semantics`` and again at
# ``pipeline_decision_artifact_hash``, so the doctrine routed the planner into a
# card that could never be minted.
#
# Unlike the two plugin-bound terms above, a gate is a NODE TYPE and carries no
# plugin at all (``NodeSpec.plugin`` is None for structural nodes), so this term
# binds on ``node_type == "gate"``.
GATE_CONDITION_AUTHORED_USER_TERM: Final[str] = "gate_condition_authored"

# The public composer may author only these pipeline-decision rows.  The full
# registry above also contains server-staged disclosures, so teaching that set
# to the model would invite it to forge server authority.
COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS: Final[frozenset[str]] = REGISTERED_PIPELINE_DECISION_USER_TERMS - {
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
}


class ServerStagedRequiredControlUserTerm(str):
    """Nominal in-process authority for a server-staged auto-wire disclosure.

    Public composer payloads can carry only the ordinary string value. The
    required-control finalizer uses this owned subtype while the candidate is
    admitted; persisted canonical rows intentionally return to plain JSON
    strings and use the existing internal-revalidation path thereafter.
    """


def composer_pipeline_decision_user_term_error(*, user_term: str, context: str) -> str | None:
    """Return bounded repair guidance for a model-authored decision term.

    The rejected value is Tier-3 text and is deliberately not reflected.  A
    closest match is selected only from the closed public vocabulary, so the
    repair remains actionable without leaking or teaching server-only terms.
    """

    normalized = user_term.strip()
    if normalized in COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS:
        return None
    allowed = sorted(COMPOSER_AUTHORED_PIPELINE_DECISION_USER_TERMS)
    closest = get_close_matches(normalized, allowed, n=1, cutoff=0.5)
    closest_guidance = f"; closest registered term: {closest[0]!r}" if closest else ""
    return (
        f"{context}: pipeline_decision user_term is not registered for composer authoring. "
        f"Available registered terms: {allowed}{closest_guidance}. Use one exactly, or remove the "
        "pipeline_decision requirement and record a novel rationale in metadata.description."
    )


PROMPT_SHIELD_WARNING_DRAFT: Final[str] = (
    "Recommend inserting a prompt-injection shield transform "
    "between the untrusted-content producer and this LLM. The current draft routes "
    "untrusted or externally controlled upstream content directly into the LLM without that shield, "
    "which is a prompt-injection exposure, but continuing without it is allowed. "
)
PROMPT_SHIELD_AVAILABLE_DRAFT: Final[str] = (
    "An authorized prompt-injection shield IS available in this deployment. Wire "
    "it between the untrusted-content producer and this LLM: untrusted or externally "
    "controlled upstream content routed straight into the LLM is a prompt-injection exposure, and the "
    "shield is configured and ready to use. Wiring it in is strongly recommended, "
    "but you may proceed without it. "
)
# Provenance-honest siblings for an LLM with no declared untrusted-content
# producer upstream. The constants above describe a declared producer; staged
# verbatim onto an operator-supplied-data pipeline that claim would be false
# for the graph. Every draft-choosing site therefore selects by the closed
# ContentTrust declaration: untrusted producers present -> the constants
# above; none -> these.
PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT: Final[str] = (
    "Recommend inserting a prompt-injection shield transform "
    "in front of this LLM if its input data may carry adversarial text. This pipeline has no "
    "external-content fetch step: the LLM consumes operator-supplied source content, so the "
    "exposure is limited to adversarial text already present in that data, and continuing "
    "without a shield is allowed. "
)
PROMPT_SHIELD_LOCAL_CONTENT_AVAILABLE_DRAFT: Final[str] = (
    "An authorized prompt-injection shield IS available in this deployment. This pipeline has no "
    "external-content fetch step: the LLM consumes operator-supplied source content, so the "
    "exposure is limited to adversarial text already present in that data. Wiring the shield in "
    "front of this LLM is recommended if that data may carry adversarial text, and you may "
    "proceed without it. "
)

_RAW_HTML_CLEANUP_DRAFT_MARKERS: Final[tuple[str, ...]] = ("raw html", "fingerprint")

# Stable prefix consumed by the set_pipeline boundary to select the closed
# error code for a term-matched cleanup row whose draft fails marker
# recognition. A term-matched row must never be silently treated as absent:
# that re-fires the missing-row contract error against a planner that DID
# author the row, which is unrepairable from the feedback alone (tutorial op
# 18b4cee7, 2026-07-22).
RAW_HTML_CLEANUP_DRAFT_MALFORMED_PREFIX: Final[str] = "Raw-html cleanup review draft is malformed"

# A pending vague_term requirement with no resolvable prompt wiring stages a
# review the operator can approve but never resolve: the resolver dead-ends
# (or silent-drops) and the execution gate counts the requirement as pending
# forever, blocking Run with no card offered. The review-staging tool rejects
# this shape at its own boundary; ``set_pipeline`` and the other node-authoring
# tools are second doors into the same state and enforce it via
# :func:`composition_review_contract_error` (session 4c42a794, 2026-09-01).
VAGUE_TERM_UNWIRED_PREFIX: Final[str] = "Pending vague_term review is not wired for resolution"

# Honest-provenance sentinel prefix for interpretation event rows written by a
# BACKEND surfacer (finalization PT auto-surface, kind-general settlement
# surfacer, YAML-import surfacer) rather than an LLM tool call. Consumers use
# it to tell server obligations apart from LLM surfacing invocations — e.g.
# the interpretation rate-cap counters exclude backend-stamped rows because
# the caps throttle LLM churn, never server-staged obligations
# (elspeth-558fa5a321).
BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX: Final[str] = "backend_auto_surface:"

AUTHORING_METADATA_OPTION_KEYS: frozenset[str] = frozenset(
    {
        INTERPRETATION_REQUIREMENTS_KEY,
        PROMPT_TEMPLATE_PARTS_KEY,
        SOURCE_AUTHORING_KEY,
    }
)

# The requirement fields the per-turn planner context keeps when it reduces a
# canonical row (elspeth-c67fbbbd83): enough to keep resolved/pending review
# state legible without echoing resolver-owned linkage (event ids, accepted
# values, artifact hashes) back to the provider. The echo-tolerant write gates
# match a supplied row against this projection as well as against the full
# stored row, so the two surfaces must share one field list.
PLANNER_CONTEXT_INTERPRETATION_REQUIREMENT_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "kind",
    "user_term",
    "draft",
    "status",
)


def project_planner_context_interpretation_requirement(requirement: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce one requirement row to its planner-context projection."""
    return {field: deep_thaw(requirement[field]) for field in PLANNER_CONTEXT_INTERPRETATION_REQUIREMENT_FIELDS if field in requirement}


def source_component_id(source_name: str) -> str:
    """Return the audit-visible component id for one composition source key."""
    return SOURCE_COMPONENT_ID if source_name == SOURCE_COMPONENT_ID else f"{SOURCE_COMPONENT_ID}:{source_name}"


def source_name_from_component_id(component_id: str) -> str | None:
    """Resolve an audit-visible source component id back to its map key."""
    if component_id == SOURCE_COMPONENT_ID:
        return SOURCE_COMPONENT_ID
    prefix = f"{SOURCE_COMPONENT_ID}:"
    if component_id.startswith(prefix):
        source_name = component_id.removeprefix(prefix)
        return source_name or None
    return None


class InterpretationRequirement(TypedDict):
    id: str
    kind: str
    user_term: str
    status: Literal["pending", "resolved"]
    draft: str | None
    event_id: str | None
    accepted_value: str | None
    accepted_artifact_hash: str | None
    resolved_prompt_template_hash: str | None


ResolvedReviewEvidenceField = Literal[
    "accepted_artifact_hash",
    "resolved_prompt_template_hash",
]

_ARTIFACT_BOUND_INTERPRETATION_KINDS: Final[frozenset[InterpretationKind]] = frozenset(
    {
        InterpretationKind.INVENTED_SOURCE,
        InterpretationKind.PIPELINE_DECISION,
        InterpretationKind.SOURCE_DATA_CONTRACT,
    }
)


def resolved_review_evidence_field(kind: InterpretationKind) -> ResolvedReviewEvidenceField:
    """Return the one hash field a resolved review kind must carry."""
    if kind in _ARTIFACT_BOUND_INTERPRETATION_KINDS:
        return "accepted_artifact_hash"
    return "resolved_prompt_template_hash"


def resolved_review_evidence_is_coherent(
    requirement: InterpretationRequirement,
    kind: InterpretationKind,
) -> bool:
    """Whether one resolved row carries the complete evidence for ``kind``."""
    if requirement["status"] != "resolved":
        return False
    event_id = requirement["event_id"]
    if type(event_id) is not str or not event_id.strip():
        return False
    if type(requirement["accepted_value"]) is not str:
        return False
    evidence_field = resolved_review_evidence_field(kind)
    other_evidence_field: ResolvedReviewEvidenceField = (
        "resolved_prompt_template_hash" if evidence_field == "accepted_artifact_hash" else "accepted_artifact_hash"
    )
    evidence = requirement[evidence_field]
    return type(evidence) is str and bool(evidence.strip()) and requirement[other_evidence_field] is None


class PromptTextPart(TypedDict):
    kind: Literal["text"]
    text: str


class PromptInterpretationRefPart(TypedDict):
    kind: Literal["interpretation_ref"]
    requirement_id: str


class PromptPart(TypedDict):
    kind: str
    text: NotRequired[str]
    requirement_id: NotRequired[str]


class SourceAuthoringMetadata(TypedDict):
    modality: str
    content_hash: str
    review_event_id: str | None
    resolved_kind: str | None


@dataclass(frozen=True, slots=True)
class InterpretationReviewSite:
    component_id: str
    component_type: Literal["source", "transform"]
    user_term: str
    kind: InterpretationKind


@dataclass(frozen=True, slots=True)
class InterpretationReviewPending:
    """Execution/readiness blocker for unresolved interpretation review."""

    sites: Sequence[InterpretationReviewSite]

    def __post_init__(self) -> None:
        freeze_fields(self, "sites")


class InterpretationReviewIntegrityError(ValueError):
    """Resolved review evidence no longer matches the artifact it attested.

    Raised by the strict execution materializer's drift guards. It carries
    the component and review kind so callers can build a structured refusal
    (409 on /execute, a readiness blocker on /validate) without echoing the
    raw integrity message, which may name hash domains. It subclasses
    ``ValueError`` so every existing ``except ValueError`` catcher (the
    reconcile-calling composer tools, the tolerant identity lane) keeps its
    behaviour; handlers that need the distinction must sit above those arms.
    """

    def __init__(
        self,
        message: str,
        *,
        component_id: str,
        component_type: Literal["source", "transform"],
        kind: InterpretationKind,
    ) -> None:
        super().__init__(message)
        self.component_id = component_id
        self.component_type: Literal["source", "transform"] = component_type
        self.kind = kind


def strip_authoring_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return runtime options with web-only authoring metadata removed."""

    return {key: value for key, value in options.items() if key not in AUTHORING_METADATA_OPTION_KEYS}


@trust_boundary(
    tier=3,
    source="NodeSpec.options, an untyped Mapping[str, Any] persisted on the composer node and "
    "round-tripped through sessions.db storage, a composer LLM tool call, or YAML import; condition/"
    "routes are typed NodeSpec fields handled nominally, not through this boundary — see the docstring "
    "note above the gate-condition arm",
    source_param="options",
    suppresses=("R5",),
    invariant="raises ValueError on every malformed field read from options across the web_scrape http "
    "identity and raw-html-cleanup mapping arms; never substitutes a default for a present-but-malformed "
    "field",
    test_ref="tests/unit/web/test_interpretation_state.py::test_validate_pipeline_decision_semantics_rejects_malformed_http_mapping",
    test_fingerprint="2a5d50996e14068729a406e3abc8872a5afc35ff8cff068a81a2e7cfca3e74ff",
)
def validate_pipeline_decision_semantics(
    *,
    node_id: str,
    plugin: str | None,
    node_type: str,
    options: Mapping[str, Any],
    condition: str | None,
    routes: Mapping[str, str] | None,
    user_term: str,
    draft: str | None,
    context: str,
    web_scrape_raw_fields: frozenset[str],
) -> None:
    """Validate that reviewed pipeline-shaping decisions match node behavior.

    Every registered term needs a binding arm here. A registered term that
    falls through validates on ANY node, which lets a misplaced row pass
    ``set_pipeline`` and mint a card that only fails later at
    ``pipeline_decision_artifact_hash`` — displacing the unresolvable-card wedge
    downstream instead of preventing it. ``node_type``/``condition``/``routes``
    are required rather than defaulted for the same reason: a caller that omits
    the gate facts would silently skip the gate arm.
    """

    normalized_term = user_term.strip()
    if normalized_term not in REGISTERED_PIPELINE_DECISION_USER_TERMS:
        # The resolve-side artifact-hash registry raises on unknown terms, so
        # accepting a novel term here mints an interpretation event that can
        # never be resolved — the session wedges at the run gate.
        raise ValueError(
            f"{context}: pipeline_decision user_term {user_term!r} is not a registered decision kind; "
            f"registered kinds: {sorted(REGISTERED_PIPELINE_DECISION_USER_TERMS)}. Novel decisions cannot "
            "be reviewed or resolved — drop the requirement and record the rationale in "
            "metadata.description, or use an llm_prompt_template review for prompt-shaped decisions."
        )
    if _is_gate_condition_authored_decision(user_term=user_term):
        if node_type != "gate":
            raise ValueError(
                f"{context}: authored gate-condition decision must be implemented by a gate node; "
                f"node {node_id!r} has node_type {node_type!r}"
            )
        # There must be something to adjudicate. Composer Stage 1 already
        # requires BOTH fields on every gate (``gate_missing_condition`` /
        # ``gate_missing_routes``), so requiring them here cannot reject a gate
        # the composer would otherwise accept — this arm is never stricter than
        # the legality rules, it only refuses to pin an empty artifact.
        #
        # These are NOMINAL checks, not structural ones: ``condition`` and
        # ``routes`` are typed fields of the owned ``NodeSpec`` dataclass, so
        # ADR-032 says type them nominally. The sibling arms below reach for
        # ``isinstance`` only because they parse ``options``, an untyped
        # Tier-3 ``Mapping[str, Any]`` blob — a different trust domain.
        if condition is None or not condition.strip():
            raise ValueError(f"{context}: authored gate-condition decision requires a non-empty condition on gate {node_id!r}")
        if routes is None:
            raise ValueError(f"{context}: authored gate-condition decision requires a routes mapping on gate {node_id!r}")
        return

    if _is_web_scrape_http_identity_decision(user_term=user_term):
        if plugin != "web_scrape":
            raise ValueError(
                f"{context}: web-scrape HTTP identity decision must be implemented by a web_scrape node; "
                f"node {node_id!r} has plugin {plugin!r}"
            )
        http = options["http"] if "http" in options else None
        if not isinstance(http, Mapping):
            raise ValueError(f"{context}: web-scrape HTTP identity decision requires options.http on node {node_id!r}")
        for field_name in ("abuse_contact", "scraping_reason"):
            value = http[field_name] if field_name in http else None
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{context}: web-scrape HTTP identity decision requires non-empty http.{field_name} on node {node_id!r}")
        return

    if not _is_raw_html_cleanup_decision(user_term=user_term, draft=draft):
        return
    if plugin != "field_mapper":
        raise ValueError(
            f"{context}: raw-html cleanup decision must be implemented by a field_mapper node; node {node_id!r} has plugin {plugin!r}"
        )
    if "select_only" not in options or options["select_only"] is not True:
        raise ValueError(f"{context}: raw-html cleanup decision requires field_mapper.select_only=true on node {node_id!r}")
    mapping = options["mapping"] if "mapping" in options else None
    if not isinstance(mapping, Mapping) or not mapping:
        raise ValueError(f"{context}: raw-html cleanup decision requires a non-empty field_mapper.mapping on node {node_id!r}")
    preserved_raw_fields = sorted(
        {
            field_name
            for source_field, target_field in mapping.items()
            for field_name in _validated_mapping_pair(source_field, target_field, context=context, node_id=node_id)
            if _looks_like_raw_html_field(field_name) or field_name in web_scrape_raw_fields
        }
    )
    if preserved_raw_fields:
        raise ValueError(
            f"{context}: raw-html cleanup decision preserves raw HTML/fingerprint field(s) "
            f"on node {node_id!r}: {preserved_raw_fields}. Remove them from mapping when select_only=true."
        )


def validate_pipeline_decision_node_semantics(
    *,
    node: NodeSpec,
    all_nodes: Sequence[NodeSpec],
    user_term: str,
    draft: str | None,
    context: str,
) -> None:
    """Validate a pipeline decision using full-state context for raw fields."""

    validate_pipeline_decision_semantics(
        node_id=node.id,
        plugin=node.plugin,
        node_type=node.node_type,
        options=node.options,
        condition=node.condition,
        routes=node.routes,
        user_term=user_term,
        draft=draft,
        context=context,
        web_scrape_raw_fields=_web_scrape_raw_fields(all_nodes),
    )


@trust_boundary(
    tier=3,
    source="CompositionState.nodes[].options['mapping'] / ['select_only'], untyped Mapping[str, Any] "
    "entries persisted on composer nodes and round-tripped through sessions.db storage, a composer LLM "
    "tool call, or YAML import; the state's own typed fields are nominal ELSPETH-owned data and are not "
    "part of this boundary",
    source_param="state",
    suppresses=("R5",),
    invariant="a node whose options['mapping'] is present but not a Mapping, or is empty, is skipped as "
    "un-analysable rather than coerced to {} — so it can never be credited here with dropping a web-scrape "
    "raw field. Non-raising, and deliberately NOT the review gate: this function only reports a "
    "composer-facing CONTRADICTION, and it returns None for a node it could not analyse. The requirement "
    "that such a node still carry a cleanup review is enforced independently by "
    "_missing_raw_html_cleanup_review_sites (reached via interpretation_sites), which is fail-safe in the "
    "same direction — a malformed mapping yields a smaller preserved-field set there and so still emits the "
    "review site.",
    non_raising=True,
)
def raw_html_cleanup_review_contract_error(state: CompositionState) -> str | None:
    """Return a composer-facing error for unreviewed or contradictory raw cleanup."""
    web_scrape_raw_fields = _web_scrape_raw_fields(state.nodes)
    if not web_scrape_raw_fields:
        return None
    for node in state.nodes:
        requirement_error = _raw_html_cleanup_requirement_contract_error(node, web_scrape_raw_fields=web_scrape_raw_fields)
        if requirement_error is not None:
            return requirement_error
    for node in state.nodes:
        if node.plugin != "field_mapper" or "select_only" not in node.options or node.options["select_only"] is not True:
            continue
        mapping = node.options["mapping"] if "mapping" in node.options else None
        if not isinstance(mapping, Mapping) or not mapping:
            continue
        preserved_fields = _preserved_mapping_fields(mapping)
        preserved_raw_fields = sorted(field for field in web_scrape_raw_fields if field in preserved_fields)
        if preserved_raw_fields and _looks_like_cleanup_node_id(node.id):
            return (
                f"Node {node.id!r} is named like raw HTML cleanup but preserves web-scrape raw field(s) "
                f"{preserved_raw_fields}. Remove those fields from field_mapper.mapping when select_only=true."
            )
        if preserved_raw_fields:
            continue
        requirements = _requirements(node.options)
        if _raw_html_cleanup_requirement(requirements) is None:
            return (
                f"Node {node.id!r} drops web-scrape raw field(s) {sorted(web_scrape_raw_fields)} with "
                "field_mapper.select_only=true. Stage a pending pipeline_decision interpretation_requirements "
                f"entry on that node with user_term {RAW_HTML_CLEANUP_USER_TERM!r} and draft "
                f"{RAW_HTML_CLEANUP_REVIEW_DRAFT!r}, then call request_interpretation_review. "
                "Do not put interpretation_requirements inside field_mapper.mapping; mapping contains "
                "only data fields to preserve. interpretation_requirements must be a sibling of mapping "
                "inside the field_mapper options object. "
                "If this came from a rejected set_pipeline call, resubmit the full pipeline with that "
                "requirement on the cleanup node; rejected set_pipeline calls do not persist partial nodes."
            )
    return None


def composition_review_contract_error(state: CompositionState) -> str | None:
    """Return the first state-level interpretation-review *contract* error.

    Only blocking contracts are aggregated here. The prompt-injection-shield
    recommendation is advisory, not blocking (see
    :func:`prompt_shield_recommendation_warning_pairs`): an unshielded
    LLM-over-untrusted-content composition surfaces a warning rather than
    failing the contract. Composition is therefore gated on the
    raw-HTML-cleanup review contract and on every staged pending
    ``vague_term`` review being resolvable.
    """

    error = raw_html_cleanup_review_contract_error(state)
    if error is not None:
        return error
    return unwired_vague_term_error(state)


@trust_boundary(
    tier=3,
    source="NodeSpec.options['interpretation_requirements'] rows, untyped Mapping[str, Any] entries "
    "persisted on composer state and round-tripped through sessions.db storage",
    source_param="state",
    suppresses=("R5",),
    invariant="a malformed row is skipped (unconditional invariant B rejects it downstream) rather than "
    "raised on; the only outputs are None or an error string naming a well-formed unwired row, so the "
    "lenient reads can only under-report, never admit an unresolvable requirement",
    non_raising=True,
)
def unwired_vague_term_error(state: CompositionState) -> str | None:
    """Return the first pending ``vague_term`` review that nothing can resolve.

    ``vague_term_wiring_count`` is the single resolvability contract: a
    pending requirement is resolvable only when exactly one wiring exists for
    its ``user_term`` (a ``prompt_template_parts`` ``interpretation_ref``
    naming its id, or — with no requirement rows staged — exactly one legacy
    placeholder). The review-staging tool already refuses to stage an unwired
    requirement; this enforces the same invariant at the node-authoring doors
    (``set_pipeline`` / ``upsert_node`` / ``splice_transform`` /
    ``patch_node_options``), which session 4c42a794 (2026-09-01) proved could
    commit the unresolvable shape directly: the requirement stayed pending
    forever, the execution gate blocked Run on it, and no resolver card
    existed. Reads are lenient (Tier-3 staging idiom, mirroring
    ``vague_term_wiring_count``): a malformed row is invariant B's to reject,
    not this contract's.
    """

    for node in state.nodes:
        if INTERPRETATION_REQUIREMENTS_KEY not in node.options:
            continue
        requirements = node.options[INTERPRETATION_REQUIREMENTS_KEY]
        if not isinstance(requirements, (list, tuple)):
            continue
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                continue
            if "kind" not in requirement or requirement["kind"] != InterpretationKind.VAGUE_TERM.value:
                continue
            if "status" not in requirement or requirement["status"] != "pending":
                continue
            if "user_term" not in requirement or not isinstance(requirement["user_term"], str):
                continue
            user_term = requirement["user_term"]
            if vague_term_wiring_count(node.options, user_term=user_term) == 1:
                continue
            requirement_id = requirement["id"] if "id" in requirement and isinstance(requirement["id"], str) else user_term
            return (
                f"{VAGUE_TERM_UNWIRED_PREFIX} on node {node.id!r}: requirement {requirement_id!r} "
                f"(user term {user_term!r}) has no resolvable prompt wiring. Wire exactly one "
                f'prompt_template_parts entry {{"kind": "interpretation_ref", "requirement_id": '
                f"{requirement_id!r}}} into the node's prompt, or drop the requirement row. An "
                "unwired requirement stages a review the operator can approve but never resolve, "
                "and the execution gate blocks Run on it forever."
            )
    return None


def prompt_shield_recommendation_warning_pairs(
    state: CompositionState,
    *,
    shield_available: bool | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return always-on advisory warnings for unshielded LLM nodes.

    The review is now ALWAYS-ON per LLM node, decoupled from whether an
    untrusted-content producer is upstream:

    - **State A** (an authorized shield is reachable upstream) — silent, no warning.
    - **State B** (``shield_available is True``) — an authorized shield IS
      configured for this deployment; strong "wire it in" advisory.
    - **State C** (``shield_available`` is ``False`` or ``None``) — no shield
      available, or availability is undeterminable; high-risk "reconsider"
      advisory. ``None`` is the FAIL-SAFE default because the pure
      :meth:`CompositionState.validate` caller has no deployment/secret context
      to distinguish B from C.

    Advisory only — the caller appends these at "medium" severity into the
    ``warnings`` list and they are excluded from the blocking contract.
    """

    graph = _output_stream_graph(state.nodes)
    warnings: list[tuple[str, str]] = []
    for node in state.nodes:
        if not node_has_capability(node, PluginCapability.LLM):
            continue
        if _llm_has_authorized_shield_upstream(node, graph):
            continue  # State A — already shielded, silent
        if _llm_has_shield_recommendation(node):
            continue  # review already staged on this node
        untrusted_producers = _llm_untrusted_content_producers(node, graph)
        if untrusted_producers:
            # Name the producer actually found. Hardcoding "web_scrape" made the
            # sentence assert a plugin that need not be in the pipeline at all.
            named = " and ".join(sorted(untrusted_producers))
            lead = (
                f"LLM node {node.id!r} consumes untrusted or externally controlled upstream content "
                f"produced by {named} without an authorized prompt-injection shield between them. "
            )
            draft = PROMPT_SHIELD_AVAILABLE_DRAFT if shield_available is True else PROMPT_SHIELD_WARNING_DRAFT
        else:
            # Provenance-honest draft: no declared untrusted producer exists in
            # this graph, so the producer-specific constants would be false —
            # and the staged review card carries the DRAFT alone, discarding
            # this computed lead, so the draft itself must tell the truth.
            lead = f"LLM node {node.id!r} has no authorized prompt-injection shield in front of it. "
            draft = PROMPT_SHIELD_LOCAL_CONTENT_AVAILABLE_DRAFT if shield_available is True else PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT
        warnings.append((f"node:{node.id}", f"{lead}{draft}"))
    return tuple(warnings)


def _llm_untrusted_content_producers(
    node: NodeSpec,
    graph: _OutputStreamGraph,
) -> frozenset[str]:
    """Return the untrusted producer plugins any predecessor path reaches unshielded.

    Empty means no such path exists, so the set is truthy exactly where the
    former boolean was True. The asymmetry is deliberate and security-critical:
    a single tainted predecessor of a queue fan-in taints the downstream LLM. A
    missing producer ends that path without reaching an untrusted producer.

    Returning the plugin NAMES rather than a bool keeps the advisory honest: it
    must name the producer it actually found, never assert a web_scrape that is
    not in the pipeline.
    """

    if not node_has_capability(node, PluginCapability.LLM):
        return frozenset()
    return _stream_reaches_untrusted(node.input, graph, frozenset())


def _stream_reaches_untrusted(stream: str | None, graph: _OutputStreamGraph, visited: frozenset[str]) -> frozenset[str]:
    if not stream:
        return frozenset()
    reached: set[str] = set()
    for producer in graph.producers_by_stream[stream] if stream in graph.producers_by_stream else ():
        reached |= _producer_reaches_untrusted(producer, graph, visited)
    return frozenset(reached)


def _is_effective_prompt_shield(node: NodeSpec) -> bool:
    """Credit only a registered transform's typed, blocking INPUT control."""
    return node_has_blocking_control(node, PluginCapability.PROMPT_SHIELD, ControlRole.INPUT)


def _producer_reaches_untrusted(producer: NodeSpec, graph: _OutputStreamGraph, visited: frozenset[str]) -> frozenset[str]:
    if producer.id in visited:
        return frozenset()
    # Path-LOCAL visited (passed by value), keyed on stable node id: a diamond
    # that reconverges on a shared upstream must not truncate a sibling path.
    visited = visited | {producer.id}
    if _is_effective_prompt_shield(producer):
        return frozenset()
    plugin = producer.plugin
    if plugin is not None and plugin in untrusted_content_transform_names():
        return frozenset({plugin})
    if producer.node_type == "queue":
        reached: set[str] = set()
        for predecessor in graph.queue_predecessors[producer.id] if producer.id in graph.queue_predecessors else ():
            reached |= _producer_reaches_untrusted(predecessor, graph, visited)
        return frozenset(reached)
    if producer.node_type == "row_union":
        reached = set()
        for branch in _coalesce_branch_connections(producer.branches):
            reached |= set(_stream_reaches_untrusted(branch, graph, visited))
        return frozenset(reached)
    return _stream_reaches_untrusted(producer.input, graph, visited)


def _llm_has_authorized_shield_upstream(
    node: NodeSpec,
    graph: _OutputStreamGraph,
) -> bool:
    """Return True iff ALL reachable predecessor paths prove an authorized shield.

    This is the always-on **State A** detector: it is judged on its own, NOT
    coupled to first reaching an untrusted producer. A shield is only credited
    when EVERY predecessor path proves one — an unshielded or unknown/missing
    predecessor path is fail-safe (NOT proven safe), so the advisory still fires.
    """

    if not node_has_capability(node, PluginCapability.LLM):
        return False
    return _stream_proves_shield(node.input, graph, frozenset())


def _stream_proves_shield(stream: str | None, graph: _OutputStreamGraph, visited: frozenset[str]) -> bool:
    if not stream:
        return False  # chain ended without a shield → not proven
    producers = graph.producers_by_stream[stream] if stream in graph.producers_by_stream else ()
    if not producers:
        return False  # missing producer → unknown → fail-safe
    return all(_producer_proves_shield(producer, graph, visited) for producer in producers)


def _producer_proves_shield(producer: NodeSpec, graph: _OutputStreamGraph, visited: frozenset[str]) -> bool:
    if producer.id in visited:
        return False  # cycle without a shield → not proven
    visited = visited | {producer.id}
    if _is_effective_prompt_shield(producer):
        return True
    if producer.plugin in untrusted_content_transform_names():
        return False
    if producer.node_type == "queue":
        predecessors = graph.queue_predecessors[producer.id] if producer.id in graph.queue_predecessors else ()
        if not predecessors:
            return False  # queue with no known predecessor → unknown → fail-safe
        return all(_producer_proves_shield(predecessor, graph, visited) for predecessor in predecessors)
    if producer.node_type == "row_union":
        branches = _coalesce_branch_connections(producer.branches)
        return bool(branches) and all(_stream_proves_shield(branch, graph, visited) for branch in branches)
    return _stream_proves_shield(producer.input, graph, visited)


def prompt_shield_state_for_node(
    node: NodeSpec,
    all_nodes: Sequence[NodeSpec],
    *,
    shield_available: bool,
) -> str:
    """Return the prompt-shield review state for ``node``: ``"A"`` / ``"B"`` / ``"C"``.

    - ``"A"`` — an authorized shield is reachable upstream (silent; no advisory).
    - ``"B"`` — no upstream shield, but an authorized shield IS available in this
      deployment (``shield_available is True``): strong "wire it in" advisory.
    - ``"C"`` — no upstream shield and no shield available (``shield_available is
      False``): high-risk "reconsider" advisory.

    The caller resolves ``shield_available`` from the principal's frozen plugin
    snapshot; the contract default when availability is undeterminable is
    ``False`` (State C, fail-safe).
    """

    if not node_has_capability(node, PluginCapability.LLM):
        return "A"
    graph = _output_stream_graph(all_nodes)
    if _llm_has_authorized_shield_upstream(node, graph):
        return "A"
    return "B" if shield_available else "C"


@trust_boundary(
    tier=3,
    source="warnings, a Sequence[Mapping[str, Any]] of already-serialised confirm_wiring wire-turn "
    "payload entries authored by the composer LLM tool call",
    source_param="warnings",
    suppresses=("R5",),
    invariant="a non-string 'message' entry passes through unmodified rather than raising; this is a "
    "cosmetic wording-refinement pass, not a security gate, so a malformed entry is inert",
    non_raising=True,
)
def refine_prompt_shield_warnings_for_availability(
    warnings: Sequence[Mapping[str, Any]],
    *,
    shield_available: bool,
) -> list[dict[str, Any]]:
    """Post-process already-serialised wire-turn warnings for B-vs-C shield state.

    ``validate()`` always emits State-C wording (``PROMPT_SHIELD_WARNING_DRAFT``)
    because it is called without deployment context.  This function upgrades
    those entries to State-B wording (``PROMPT_SHIELD_AVAILABLE_DRAFT``) when the
    route layer has confirmed that the authorized shield is present.

    Non-shield warnings and the ``severity`` / ``component`` fields are
    passed through unchanged.  The input sequence is not mutated.

    Args:
        warnings: Sequence of already-serialised warning dicts
            (``{"component": str, "message": str, "severity": str}``).
        shield_available: ``True`` iff the request snapshot selected a usable
            prompt-shield implementation.
    """
    result: list[dict[str, Any]] = [dict(entry) for entry in warnings]
    if not shield_available:
        return result
    # C->B upgrades per provenance variant; the replace pairs must stay
    # variant-aligned so a local-content warning never acquires the
    # producer-specific untrusted-content claim.
    upgrades = (
        (PROMPT_SHIELD_WARNING_DRAFT, PROMPT_SHIELD_AVAILABLE_DRAFT),
        (PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT, PROMPT_SHIELD_LOCAL_CONTENT_AVAILABLE_DRAFT),
    )
    for entry in result:
        message = entry["message"] if "message" in entry else None
        if not isinstance(message, str):
            continue
        for c_draft, b_draft in upgrades:
            if c_draft in message:
                entry["message"] = message.replace(c_draft, b_draft)
                break
    return result


def _llm_has_shield_recommendation(node: NodeSpec) -> bool:
    requirements = _requirements(node.options)
    if requirements is None:
        return False
    for requirement in requirements:
        if InterpretationKind(requirement["kind"]) is not InterpretationKind.PIPELINE_DECISION:
            continue
        if requirement["user_term"].strip() == PROMPT_SHIELD_USER_TERM:
            return True
    return False


def _raw_html_cleanup_requirement_contract_error(
    node: NodeSpec,
    *,
    web_scrape_raw_fields: frozenset[str],
) -> str | None:
    try:
        requirements = _requirements(node.options)
    except (KeyError, TypeError, ValueError) as exc:
        return f"Node {node.id!r} has invalid interpretation_requirements: {exc}"
    if requirements is None:
        return None
    for requirement in requirements:
        if InterpretationKind(requirement["kind"]) is not InterpretationKind.PIPELINE_DECISION:
            continue
        if not _is_raw_html_cleanup_decision(user_term=requirement["user_term"], draft=requirement["draft"]):
            if requirement["user_term"].strip() == RAW_HTML_CLEANUP_USER_TERM:
                return (
                    f"{RAW_HTML_CLEANUP_DRAFT_MALFORMED_PREFIX} on node {node.id!r}: the row's "
                    f"user_term matches {RAW_HTML_CLEANUP_USER_TERM!r} but its draft text is not "
                    "recognized. The draft must contain both 'raw html' and 'fingerprint'. "
                    f"Copy the canonical draft verbatim, do not rephrase: {RAW_HTML_CLEANUP_REVIEW_DRAFT!r}"
                )
            continue
        try:
            validate_pipeline_decision_semantics(
                node_id=node.id,
                plugin=node.plugin,
                node_type=node.node_type,
                options=node.options,
                condition=node.condition,
                routes=node.routes,
                user_term=requirement["user_term"],
                draft=requirement["draft"],
                context="raw-html cleanup review contract",
                web_scrape_raw_fields=web_scrape_raw_fields,
            )
        except ValueError as exc:
            return str(exc)
    return None


def _is_raw_html_cleanup_decision(*, user_term: str, draft: str | None) -> bool:
    normalized_term = user_term.strip()
    if normalized_term != RAW_HTML_CLEANUP_USER_TERM:
        return False
    if draft is None:
        return False
    normalized_draft = draft.lower()
    return all(marker in normalized_draft for marker in _RAW_HTML_CLEANUP_DRAFT_MARKERS)


def _is_web_scrape_http_identity_decision(*, user_term: str) -> bool:
    return user_term.strip() == WEB_SCRAPE_HTTP_IDENTITY_USER_TERM


def _is_gate_condition_authored_decision(*, user_term: str) -> bool:
    return user_term.strip() == GATE_CONDITION_AUTHORED_USER_TERM


@trust_boundary(
    tier=3,
    source="one side of a node.options['mapping'] item on a field_mapper composer node — an "
    "untyped authored value persisted through sessions.db storage, a composer LLM tool call, or "
    "YAML import",
    source_param="field",
    suppresses=("R5",),
    invariant="raises ValueError whenever the mapping side is not a str; never coerces, "
    "stringifies, or substitutes a default for a malformed side",
    test_ref="tests/unit/web/test_interpretation_state.py::test_validated_mapping_field_rejects_non_string_mapping_sides",
    test_fingerprint="dbd1cadb64e0020d540e8430f2ff1412f2bb097afe9d6b6754b21b8196746ac8",
)
def _validated_mapping_field(field: object, *, context: str, node_id: str) -> str:
    if not isinstance(field, str):
        raise ValueError(f"{context}: field_mapper.mapping on node {node_id!r} must map string field names to string field names")
    return field


def _validated_mapping_pair(source_field: object, target_field: object, *, context: str, node_id: str) -> tuple[str, str]:
    return (
        _validated_mapping_field(source_field, context=context, node_id=node_id),
        _validated_mapping_field(target_field, context=context, node_id=node_id),
    )


def _looks_like_raw_html_field(field_name: str) -> bool:
    normalized = field_name.strip().lower().replace("-", "_")
    return normalized == "content" or "html" in normalized or "fingerprint" in normalized


def _looks_like_cleanup_node_id(node_id: str) -> bool:
    normalized = node_id.strip().lower().replace("-", "_")
    return "drop" in normalized or "cleanup" in normalized or "clean" in normalized or "raw" in normalized or "html" in normalized


@trust_boundary(
    tier=3,
    source="the items of node.options['mapping'], an untyped Mapping[str, Any] persisted on a "
    "field_mapper composer node and round-tripped through sessions.db storage, a composer LLM tool call, "
    "or YAML import",
    source_param="mapping",
    suppresses=("R5",),
    invariant="collects only entries that are genuinely str; a non-string key or value contributes "
    "nothing rather than being coerced or stringified, so the returned frozenset never names a field the "
    "authored mapping does not carry. Non-raising: its one caller, "
    "raw_html_cleanup_review_contract_error, reads a smaller set as 'this raw field is NOT preserved', "
    "which falls through to that function's review-requirement check rather than emitting a "
    "preserves-raw-fields contradiction. The sibling enumerator "
    "_missing_raw_html_cleanup_review_sites deliberately does NOT call this helper: it needs the STRICTER "
    "both-sides-str pair filter, and widening it to this helper's per-side collection would grow its "
    "preserved set and suppress review sites.",
    non_raising=True,
)
def _preserved_mapping_fields(mapping: Mapping[str, Any]) -> frozenset[str]:
    fields: set[str] = set()
    for source_field, target_field in mapping.items():
        if isinstance(source_field, str):
            fields.add(source_field.strip())
        if isinstance(target_field, str):
            fields.add(target_field.strip())
    return frozenset(fields)


@trust_boundary(
    tier=3,
    source="NodeSpec.options[key], an untyped Mapping[str, Any] persisted on the composer node and "
    "round-tripped through sessions.db storage, a composer LLM tool call, or YAML import; key names one "
    "of the scalar string-valued options (model, prompt_template, profile, content_field, "
    "fingerprint_field) — the node's other, typed fields are nominal ELSPETH-owned data and are not part "
    "of this boundary",
    source_param="node",
    suppresses=("R5",),
    invariant="returns the value only when the key is present AND holds a str, and None in every other "
    "case; a present-but-non-string value is reported as absent rather than coerced or stringified, so no "
    "caller can observe a non-str. Non-raising by contract: every caller reads None as 'this node has no "
    "authored value for key' and takes the same branch an absent key already took, which in each case is "
    "the conservative one (no materialization, no review site suppressed, no field credited as preserved).",
    non_raising=True,
)
def _node_str_option(node: NodeSpec, key: str) -> str | None:
    """Read one string-valued option off a composer node's Tier-3 options map.

    The single parse point for the scalar string options this module reads.
    Callers consume the owned ``str | None`` and never re-interrogate the
    option's runtime type.
    """
    value = node.options[key] if key in node.options else None
    return value if isinstance(value, str) else None


def _node_option_is_non_text(node: NodeSpec, key: str) -> bool:
    """True when ``key`` is present with a non-null value that is not text.

    The shape an ``inline_content`` blob marker takes in an LLM prompt or
    model field. Built on :func:`_node_str_option` (the one parse point) plus
    a membership test, so the execution materializer can tell "no value" from
    "a value a resolved review cannot have attested" without re-interrogating
    the option type. An explicit null stays the absent value.
    """
    return key in node.options and node.options[key] is not None and _node_str_option(node, key) is None


def _refuse_resolved_review_over_non_text(node: NodeSpec, *, option_key: str, kind: InterpretationKind) -> None:
    """Drift backstop: a RESOLVED prompt/model review over a non-text value.

    A resolved ``llm_prompt_template`` / ``llm_model_choice`` review attested a
    concrete string. A non-text value in that field (an ``inline_content`` blob
    marker, substituted only at run time) is not what was reviewed, and the
    string drift guards never run because there is no string to hash. This is
    drift, not debt: no card can attest a marker (the surfacer, event writer
    and resolver all read the option as text), so it is refused here rather
    than enumerated as a pending site nothing can clear.

    A marker with NO resolved review is not refused: LLM-authored blobs are
    refused in these fields at wire time and at run admission, so what remains
    is user-verbatim content (ADR-034) with no composer review to stage.
    """
    if not _node_option_is_non_text(node, option_key):
        return
    if _resolved_requirement_for_kind(_requirements(node.options), kind) is None:
        return
    raise InterpretationReviewIntegrityError(
        f"llm node {node.id!r} {kind.value} review is resolved but options.{option_key} is no longer text",
        component_id=node.id,
        component_type="transform",
        kind=kind,
    )


def _raw_html_cleanup_requirement(requirements: Sequence[InterpretationRequirement] | None) -> InterpretationRequirement | None:
    if requirements is None:
        return None
    for requirement in requirements:
        if InterpretationKind(requirement["kind"]) is not InterpretationKind.PIPELINE_DECISION:
            continue
        if _is_raw_html_cleanup_decision(user_term=requirement["user_term"], draft=requirement["draft"]):
            return requirement
    return None


def interpretation_sites(
    state: CompositionState,
    *,
    operator_resolved_model_node_ids: frozenset[str] = frozenset(),
) -> tuple[InterpretationReviewSite, ...]:
    """Return unresolved interpretation-review sites across source and transforms.

    ``operator_resolved_model_node_ids`` identifies LLM nodes whose concrete
    model was supplied by lowering an operator-owned profile alias.  Those
    model choices are operator policy, not composer-authored decisions, so
    they do not require a user ``llm_model_choice`` review.
    """

    sites: list[InterpretationReviewSite] = []
    for source_name, source in state.sources.items():
        sites.extend(_pending_source_sites(source, component_id=source_component_id(source_name)))
    sites.extend(_pending_source_data_contract_sites(state))
    web_scrape_raw_fields = _web_scrape_raw_fields(state.nodes)
    for node in state.nodes:
        node_sites = [*_pending_node_sites(node), *_legacy_placeholder_sites(node)]
        if node.id in operator_resolved_model_node_ids:
            node_sites = [site for site in node_sites if site.kind is not InterpretationKind.LLM_MODEL_CHOICE]
        sites.extend(node_sites)
        sites.extend(_missing_raw_html_cleanup_review_sites(node, web_scrape_raw_fields=web_scrape_raw_fields))
        if not any(site.kind is InterpretationKind.LLM_PROMPT_TEMPLATE for site in node_sites):
            sites.extend(_missing_prompt_template_review_sites(node))
        if node.id not in operator_resolved_model_node_ids and not any(
            site.kind is InterpretationKind.LLM_MODEL_CHOICE for site in node_sites
        ):
            sites.extend(_missing_model_choice_review_sites(node))
    return tuple(dict.fromkeys(sites))


def transform_vague_term_site_tuples(nodes: Sequence[NodeSpec]) -> tuple[tuple[str, str], ...]:
    """Compatibility view for vague-term LLM handoff paths.

    Older prompt-repair and handoff paths consume only ``(node_id, term)``
    tuples for transform vague-term sites. Keep that legacy view mechanical
    instead of letting source or prompt-template review sites bleed into this
    narrow tuple API.
    """

    sites: list[tuple[str, str]] = []
    for node in nodes:
        for site in (*_pending_node_sites(node), *_legacy_placeholder_sites(node)):
            if site.component_type == "transform" and site.kind is InterpretationKind.VAGUE_TERM:
                sites.append((site.component_id, site.user_term))
    return tuple(dict.fromkeys(sites))


def materialize_state_for_authoring(state: CompositionState) -> CompositionState:
    """Return a validation-safe authoring state without mutating ``state``."""

    changed = False
    materialized_nodes: list[NodeSpec] = []
    for node in state.nodes:
        materialized = _materialize_node_for_authoring(node)
        materialized_nodes.append(materialized)
        changed = changed or materialized is not node
    if not changed:
        return state
    return replace(state, nodes=tuple(materialized_nodes))


def _profile_resolved_model_node_ids(state: CompositionState) -> frozenset[str]:
    """LLM nodes whose concrete model came from an operator-owned profile alias."""
    return frozenset(node.id for node in state.nodes if node.plugin == "llm" and _node_str_option(node, "profile") is not None)


def pending_execution_interpretation_sites(
    state: CompositionState,
    *,
    operator_resolved_model_node_ids: frozenset[str] = frozenset(),
) -> tuple[InterpretationReviewSite, ...]:
    """Pending interpretation-review sites as the execution gate counts them.

    Single authority for "which unresolved reviews block execution": derives
    the operator-profile model exemption from the state itself (unioned with
    any caller-supplied ids) and delegates to :func:`interpretation_sites`.
    :func:`materialize_state_for_execution` and the composer's mid-turn
    composition-state persistence (``_state_payload_for_compose_turn``) share
    this predicate so a state the mid-turn writer persists as valid can never
    carry a review the execution gate would block on — the two answers agree
    by construction (elspeth-67c6fa691d).
    """
    return interpretation_sites(
        state,
        operator_resolved_model_node_ids=operator_resolved_model_node_ids | _profile_resolved_model_node_ids(state),
    )


def materialize_state_for_execution(
    state: CompositionState,
    *,
    operator_resolved_model_node_ids: frozenset[str] = frozenset(),
) -> CompositionState | InterpretationReviewPending:
    """Materialize resolved interpretation state or return pending sites.

    The operator-profile model exemption is a property of the state — an LLM
    node bound to a ``profile`` alias took its concrete model from operator
    policy, not composer authoring, so it carries no ``llm_model_choice`` review
    card to resolve. Derive that set here (unioned with any caller-supplied ids)
    rather than trusting every caller to compute and pass it: the run-readiness
    path (``execution.validation``) did, but the execute path
    (``execution.service``) called with no argument, so a profile-aliased node
    with a stale pending ``llm_model_choice`` review passed readiness yet 422'd at
    /execute (freeform session 0c59fbca). Deriving it makes the two agree by
    construction. Mirrors ``execution.validation``'s ``profile``-is-str test.
    """

    operator_resolved_model_node_ids = operator_resolved_model_node_ids | _profile_resolved_model_node_ids(state)

    pending_sites = pending_execution_interpretation_sites(
        state,
        operator_resolved_model_node_ids=operator_resolved_model_node_ids,
    )
    if pending_sites:
        return InterpretationReviewPending(sites=pending_sites)

    changed = False
    materialized_sources = dict(state.sources)
    for source_name, source in state.sources.items():
        materialized_source = _materialize_source_for_execution(source, component_id=source_component_id(source_name))
        if materialized_source is not source:
            materialized_sources[source_name] = materialized_source
            changed = True
    materialized_nodes: list[NodeSpec] = []
    for node in state.nodes:
        materialized = _materialize_node_for_execution(
            node,
            state.nodes,
            operator_resolved_model=node.id in operator_resolved_model_node_ids,
        )
        materialized_nodes.append(materialized)
        changed = changed or materialized is not node
    if not changed:
        return state
    return replace(state, sources=materialized_sources, nodes=tuple(materialized_nodes))


def _materialize_node_for_authoring(node: NodeSpec) -> NodeSpec:
    if node.plugin != "llm":
        return node
    options = node.options
    parts = _prompt_parts(options)
    if parts is not None:
        prompt = _render_prompt_parts(parts, _requirements_by_id(options), unresolved_text=PENDING_INTERPRETATION_AUTHORING_TEXT)
        return _replace_prompt_if_changed(node, prompt, include_hash=False)

    if "approved_prompt_artifact_hash" in options:
        return node

    prompt_template = _node_str_option(node, "prompt_template")
    if prompt_template is None:
        return node
    masked = INTERPRETATION_PLACEHOLDER_RE.sub(PENDING_INTERPRETATION_AUTHORING_TEXT, prompt_template)
    return _replace_prompt_if_changed(node, masked, include_hash=False)


def _materialize_node_for_execution(
    node: NodeSpec,
    all_nodes: Sequence[NodeSpec],
    *,
    operator_resolved_model: bool = False,
) -> NodeSpec:
    _validate_pipeline_decision_review(node, all_nodes)
    if node.plugin != "llm":
        return node
    model = _node_str_option(node, "model")
    if not operator_resolved_model:
        _refuse_resolved_review_over_non_text(node, option_key="model", kind=InterpretationKind.LLM_MODEL_CHOICE)
    if model and not operator_resolved_model:
        _validate_model_choice_review(node, model)
    parts = _prompt_parts(node.options)
    if parts is None:
        # Structured nodes render prompt_template from their parts, so only a
        # parts-free node can execute a non-text prompt_template.
        if multi_query_prompt_surface_from_options(node.options) is None:
            _refuse_resolved_review_over_non_text(node, option_key="prompt_template", kind=InterpretationKind.LLM_PROMPT_TEMPLATE)
        prompt_template = _node_str_option(node, "prompt_template")
        if prompt_template or multi_query_prompt_surface_from_options(node.options) is not None:
            requirement = _prompt_template_review_requirement(node.options)
            if requirement is not None:
                _validate_prompt_template_review(node, prompt_template)
                return _ensure_prompt_template_hash(node)
        if "approved_prompt_artifact_hash" in node.options and approved_prompt_artifact_hash_from_options(node.options) is None:
            options = dict(node.options)
            del options["approved_prompt_artifact_hash"]
            return replace(node, options=options)
        return node
    prompt = _render_prompt_parts(parts, _requirements_by_id(node.options), unresolved_text=None)
    _validate_prompt_template_review(node, prompt)
    return _replace_prompt_if_changed(node, prompt, include_hash=True)


def _materialize_source_for_execution(source: SourceSpec, *, component_id: str) -> SourceSpec:
    metadata = _source_authoring_metadata(source.options)
    if metadata is None or not _is_llm_authored_modality(metadata["modality"]):
        return source
    requirements = _requirements(source.options)
    resolved = _resolved_requirement_for_kind(requirements, InterpretationKind.INVENTED_SOURCE)
    if resolved is None:
        raise InterpretationReviewIntegrityError(
            "invented source review requirement is required before execution",
            component_id=component_id,
            component_type="source",
            kind=InterpretationKind.INVENTED_SOURCE,
        )
    accepted_hash = resolved["accepted_artifact_hash"]
    if accepted_hash != metadata["content_hash"]:
        raise InterpretationReviewIntegrityError(
            "invented source review drift: reviewed content hash does not match current source content hash",
            component_id=component_id,
            component_type="source",
            kind=InterpretationKind.INVENTED_SOURCE,
        )
    return source


def _replace_prompt_if_changed(node: NodeSpec, prompt: str, *, include_hash: bool) -> NodeSpec:
    current = node.options["prompt_template"] if "prompt_template" in node.options else None
    if current != prompt:
        node = replace(node, options={**node.options, "prompt_template": prompt})
    return _ensure_prompt_template_hash(node) if include_hash else node


def _is_inline_prompt_blob(value: object) -> bool:
    marker = is_widened_blob_ref(deep_thaw(value))
    return marker is not None and marker.mode == "inline_content"


def _ensure_prompt_template_hash(node: NodeSpec) -> NodeSpec:
    artifact_hash = approved_prompt_artifact_hash_from_options(node.options)
    if artifact_hash is None:
        if "approved_prompt_artifact_hash" not in node.options:
            return node
        options = dict(node.options)
        del options["approved_prompt_artifact_hash"]
        return replace(node, options=options)
    if "approved_prompt_artifact_hash" in node.options and node.options["approved_prompt_artifact_hash"] == artifact_hash:
        return node
    return replace(node, options={**node.options, "approved_prompt_artifact_hash": artifact_hash})


def _pending_source_sites(source: SourceSpec, *, component_id: str) -> tuple[InterpretationReviewSite, ...]:
    metadata = _source_authoring_metadata(source.options)
    if metadata is None or not _is_llm_authored_modality(metadata["modality"]):
        return ()
    requirements = _requirements(source.options)
    requirement = _requirement_for_kind(requirements, InterpretationKind.INVENTED_SOURCE)
    if requirement is None:
        return (
            InterpretationReviewSite(
                component_id=component_id,
                component_type="source",
                user_term="llm_generated_source",
                kind=InterpretationKind.INVENTED_SOURCE,
            ),
        )
    # A resolved invented_source is clean ONLY while its accepted artifact still
    # matches the current source content_hash. Resolved-but-drifted (the source
    # content_hash changed after the review was accepted) falls through to a
    # pending review site — the single source of truth for /validate and
    # /execute — rather than letting the downstream
    # _materialize_source_for_execution drift guard raise a bare ValueError that
    # the route layer mis-maps to a 404/500.
    if requirement["status"] == "resolved" and requirement["accepted_artifact_hash"] == metadata["content_hash"]:
        return ()
    return (
        InterpretationReviewSite(
            component_id=component_id,
            component_type="source",
            user_term=requirement["user_term"].strip(),
            kind=InterpretationKind.INVENTED_SOURCE,
        ),
    )


def _source_data_contract_requirement(options: Mapping[str, Any]) -> InterpretationRequirement | None:
    return _requirement_for_kind(_requirements(options), InterpretationKind.SOURCE_DATA_CONTRACT)


def resolved_source_data_contract_fields(requirement: InterpretationRequirement) -> tuple[str, ...] | None:
    """Return the field set bound by coherent resolved contract evidence."""
    if not resolved_review_evidence_is_coherent(requirement, InterpretationKind.SOURCE_DATA_CONTRACT):
        return None
    accepted_value = requirement["accepted_value"]
    if accepted_value is None:
        return None
    fields = parse_source_data_contract_accepted_fields(accepted_value)
    if requirement["accepted_artifact_hash"] != source_data_contract_artifact_hash(fields):
        return None
    return fields


@observation_boundary(
    tier=3,
    source="a source options schema mapping persisted in composer state, whose guaranteed_fields value "
    "may have been authored by the planner or stamped by source_data_contract resolution",
    source_param="options",
    suppresses=("R5",),
    invariant="returns a frozen string set only for an observed schema with an explicit list/tuple of "
    "string guaranteed_fields; every absent, malformed, or non-observed shape returns None and never raises",
)
def _observed_source_guaranteed_fields(options: Mapping[str, Any]) -> frozenset[str] | None:
    schema_key = "schema" if "schema" in options else ("schema_config" if "schema_config" in options else None)
    if schema_key is None:
        return None
    raw_schema = options[schema_key]
    if not isinstance(raw_schema, Mapping):
        return None
    mode = raw_schema["mode"] if "mode" in raw_schema else None
    if mode != "observed" or "guaranteed_fields" not in raw_schema:
        return None
    raw_fields = raw_schema["guaranteed_fields"]
    if not isinstance(raw_fields, (list, tuple)) or not all(isinstance(field, str) for field in raw_fields):
        return None
    return frozenset(raw_fields)


def _source_data_contract_evidence_is_current(
    source: SourceSpec,
    requirement: InterpretationRequirement,
) -> bool:
    acknowledged_fields = resolved_source_data_contract_fields(requirement)
    guaranteed_fields = _observed_source_guaranteed_fields(source.options)
    return acknowledged_fields is not None and guaranteed_fields is not None and frozenset(acknowledged_fields) <= guaranteed_fields


def current_source_data_contract_demand(state: CompositionState, source_name: str) -> tuple[str, ...]:
    """Requirement-aware demand backtrace for one source.

    The single derivation shared by the pending-site enumerator, the
    event-writer boundary, and the resolve arm (``sessions/service.py``), so
    the card, its dedup identity, and the resolution stamp cannot diverge on
    what the pipeline demands. Strips a previously ACKNOWLEDGED field set
    (parsed from the resolved requirement's ``accepted_value``) before
    recomputing, so a demand-set change after acknowledgement is measured
    against the graph rather than against the stamp the previous answer
    produced. Returns ``()`` for ineligible sources: an LLM-authored bound
    blob (``source_authoring`` present — its content IS the run's data and
    the invented_source/auto-declare flow owns it), a missing source, or a
    source that cannot carry a guarantee stamp.
    """
    source = state.sources[source_name] if source_name in state.sources else None
    if source is None or SOURCE_AUTHORING_KEY in source.options:
        return ()
    requirement = _source_data_contract_requirement(source.options)
    disregard: frozenset[str] = frozenset()
    if (
        requirement is not None
        and requirement["status"] == "resolved"
        and resolved_review_evidence_is_coherent(requirement, InterpretationKind.SOURCE_DATA_CONTRACT)
        and requirement["accepted_value"] is not None
    ):
        acknowledged = source_data_contract_fields_for_demand_recompute(
            requirement["accepted_value"],
            requirement["accepted_artifact_hash"],
        )
        disregard = frozenset(acknowledged)
    return backtraced_source_demand(state, source_name, disregard_fields=disregard)


def _pending_source_data_contract_sites(state: CompositionState) -> tuple[InterpretationReviewSite, ...]:
    """Enumerate unacknowledged (or drifted) data-contract sites per source.

    Derived, not staged (elspeth-da68332faf work item 2): the site exists
    exactly while the graph demands fields from a not-preflight-checkable
    source that no current acknowledgement covers — whether the demand
    existed at bind time or arose later from a node mutation. Mirrors the
    ``_pending_source_sites`` drift posture for invented_source: a resolved
    requirement is clean ONLY while its accepted artifact (here the
    acknowledged contract version, consequence, and FIELD SET, bound by
    ``source_data_contract_artifact_hash``)
    still matches the current demand; a demand-set change falls through to a
    pending site, re-opening the card. A demand that shrinks to EMPTY closes
    the site without re-asking: there is nothing left to acknowledge, and
    the standing stamp remains the user's own recorded promise. Independently,
    a resolved row whose evidence is incoherent or whose source no longer
    carries the acknowledged guarantee always emits a blocking integrity site,
    even when the current graph has no remaining demand. With no live demand,
    that site is fail-closed state-integrity evidence rather than a new user-
    resolvable acknowledgement card.
    """
    sites: list[InterpretationReviewSite] = []
    for source_name, source in state.sources.items():
        if SOURCE_AUTHORING_KEY in source.options:
            continue
        requirement = _source_data_contract_requirement(source.options)
        if (
            requirement is not None
            and requirement["status"] == "resolved"
            and not _source_data_contract_evidence_is_current(source, requirement)
        ):
            sites.append(
                InterpretationReviewSite(
                    component_id=source_component_id(source_name),
                    component_type="source",
                    user_term=requirement["user_term"].strip(),
                    kind=InterpretationKind.SOURCE_DATA_CONTRACT,
                )
            )
            continue
        demand = current_source_data_contract_demand(state, source_name)
        if not demand:
            continue
        if (
            requirement is not None
            and requirement["status"] == "resolved"
            and requirement["accepted_artifact_hash"] == source_data_contract_artifact_hash(demand)
        ):
            continue
        sites.append(
            InterpretationReviewSite(
                component_id=source_component_id(source_name),
                component_type="source",
                user_term=(requirement["user_term"].strip() if requirement is not None else SOURCE_DATA_CONTRACT_USER_TERM),
                kind=InterpretationKind.SOURCE_DATA_CONTRACT,
            )
        )
    return tuple(sites)


def _pending_node_sites(node: NodeSpec) -> tuple[InterpretationReviewSite, ...]:
    requirements = _requirements(node.options)
    if requirements is None:
        return ()
    sites: list[InterpretationReviewSite] = []
    for requirement in requirements:
        status = requirement["status"]
        if status == "pending":
            kind = InterpretationKind(requirement["kind"])
            if node.plugin != "llm" and kind is not InterpretationKind.PIPELINE_DECISION:
                continue
            if kind is InterpretationKind.SOURCE_DATA_CONTRACT:
                # Source-only kind: its sites derive from the graph demand in
                # _pending_source_data_contract_sites. A rogue node-staged row
                # must not mint a transform site no resolver arm can settle.
                continue
            sites.append(
                InterpretationReviewSite(
                    component_id=node.id,
                    component_type="transform",
                    user_term=requirement["user_term"].strip(),
                    kind=kind,
                )
            )
    return tuple(sites)


def _web_scrape_raw_fields(nodes: Sequence[NodeSpec]) -> frozenset[str]:
    fields: set[str] = set()
    for node in nodes:
        if node.plugin != "web_scrape":
            continue
        content_field = _node_str_option(node, "content_field")
        fingerprint_field = _node_str_option(node, "fingerprint_field")
        if content_field and content_field.strip():
            fields.add(content_field.strip())
        if fingerprint_field and fingerprint_field.strip():
            fields.add(fingerprint_field.strip())
    return frozenset(fields)


@trust_boundary(
    tier=3,
    source="NodeSpec.options['mapping'] / ['select_only'], untyped Mapping[str, Any] entries persisted "
    "on a field_mapper composer node and round-tripped through sessions.db storage, a composer LLM tool "
    "call, or YAML import",
    source_param="node",
    suppresses=("R5",),
    invariant="options['mapping'] must be a present, non-empty Mapping and each preserved field must be "
    "a genuine str->str pair; anything else contributes no preserved field. Non-raising and fail-safe in "
    "the review direction: a malformed mapping yields a SMALLER preserved-field set, which means the "
    "web-scrape raw fields read as NOT preserved and the cleanup review site is still emitted. Malformed "
    "options can therefore only ADD a required review, never suppress one.",
    non_raising=True,
)
def _missing_raw_html_cleanup_review_sites(
    node: NodeSpec,
    *,
    web_scrape_raw_fields: frozenset[str],
) -> tuple[InterpretationReviewSite, ...]:
    """Return the required review site for unreviewed web-scrape field cleanup."""
    if not web_scrape_raw_fields:
        return ()
    if node.plugin != "field_mapper":
        return ()
    requirements = _requirements(node.options)
    if _raw_html_cleanup_requirement(requirements) is not None:
        return ()
    if "select_only" not in node.options or node.options["select_only"] is not True:
        return ()
    mapping = node.options["mapping"] if "mapping" in node.options else None
    if not isinstance(mapping, Mapping) or not mapping:
        return ()
    preserved_fields = {
        field_name
        for source_field, target_field in mapping.items()
        if isinstance(source_field, str) and isinstance(target_field, str)
        for field_name in (source_field.strip(), target_field.strip())
    }
    if any(field in preserved_fields for field in web_scrape_raw_fields):
        return ()
    return (
        InterpretationReviewSite(
            component_id=node.id,
            component_type="transform",
            user_term=RAW_HTML_CLEANUP_USER_TERM,
            kind=InterpretationKind.PIPELINE_DECISION,
        ),
    )


def _legacy_placeholder_sites(node: NodeSpec) -> tuple[InterpretationReviewSite, ...]:
    if node.plugin != "llm":
        return ()
    prompt_template = _node_str_option(node, "prompt_template")
    if prompt_template is None:
        return ()
    return tuple(
        InterpretationReviewSite(
            component_id=node.id,
            component_type="transform",
            user_term=term,
            kind=InterpretationKind.VAGUE_TERM,
        )
        for term in _legacy_terms(prompt_template)
    )


def _missing_prompt_template_review_sites(node: NodeSpec) -> tuple[InterpretationReviewSite, ...]:
    """Enumerate prompt-template review debt on an LLM node as pending sites.

    A node with no ``llm_prompt_template`` requirement, or with a pending one,
    is a site. A RESOLVED review is not: a stored anchor that no longer
    matches :func:`prompt_review_anchor_hash_from_options` is drift, refused
    by the materializer (:func:`_validate_prompt_template_review`).

    A present NON-TEXT ``prompt_template`` (an ``inline_content`` blob marker)
    enumerates no site: the card surfacer, event writer and resolver all read
    the option as text, so such a site could never become a card. LLM-authored
    blobs are refused in this field at wire time and at run admission, so a
    marker with no resolved review is user-verbatim content (ADR-034). A marker
    under a RESOLVED review is drift, refused by the materializer
    (:func:`_refuse_resolved_review_over_non_text`).
    """
    if node.plugin != "llm":
        return ()
    prompt_template = _node_str_option(node, "prompt_template")
    if not prompt_template and multi_query_prompt_surface_from_options(node.options) is None:
        return ()
    requirement = _prompt_template_review_requirement(node.options)
    if requirement is None:
        return (
            InterpretationReviewSite(
                component_id=node.id,
                component_type="transform",
                user_term=f"llm_prompt_template:{node.id}",
                kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
            ),
        )
    if requirement["status"] == "resolved":
        return ()
    return (
        InterpretationReviewSite(
            component_id=node.id,
            component_type="transform",
            user_term=_requirement_user_term_or_default(requirement, "prompt_template"),
            kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        ),
    )


def _missing_model_choice_review_sites(node: NodeSpec) -> tuple[InterpretationReviewSite, ...]:
    """Enumerate an unreviewed model choice as a pending review site.

    Parallel to :func:`_missing_prompt_template_review_sites` for the
    ``llm_model_choice`` review kind. The mutation-time auto-stager
    (:func:`_options_with_default_model_choice_review`) catches new
    options-mutations; this enumerator catches pre-existing state that
    pre-dates the auto-stager (e.g. composition rows loaded from
    ``sessions.db`` before the gate was added). Together they guarantee
    that no LLM node with a non-empty ``options.model`` ever reaches a
    runnable state without a surfaced model-choice review.
    """
    if node.plugin != "llm":
        return ()
    # A non-text model (a blob marker) reads as absent and enumerates no site,
    # for the reasons given on :func:`_missing_prompt_template_review_sites`;
    # a marker under a resolved review is refused by the materializer as drift.
    model = _node_str_option(node, "model")
    if not model:
        return ()
    requirement = _model_choice_review_requirement(node.options)
    if requirement is None:
        return (
            InterpretationReviewSite(
                component_id=node.id,
                component_type="transform",
                user_term=f"llm_model_choice:{node.id}",
                kind=InterpretationKind.LLM_MODEL_CHOICE,
            ),
        )
    if requirement["status"] == "resolved":
        return ()
    return (
        InterpretationReviewSite(
            component_id=node.id,
            component_type="transform",
            user_term=_requirement_user_term_or_default(requirement, "model"),
            kind=InterpretationKind.LLM_MODEL_CHOICE,
        ),
    )


def _requirement_user_term_or_default(requirement: InterpretationRequirement | None, default: str) -> str:
    if requirement is None:
        return default
    return requirement["user_term"].strip()


def _requirements_by_id(options: Mapping[str, Any]) -> dict[str, InterpretationRequirement]:
    requirements = _requirements(options)
    if requirements is None:
        return {}
    by_id: dict[str, InterpretationRequirement] = {}
    for requirement in requirements:
        requirement_id = requirement["id"]
        if requirement_id in by_id:
            raise ValueError(f"duplicate interpretation requirement id {requirement_id!r}")
        by_id[requirement_id] = requirement
    return by_id


@trust_boundary(
    tier=3,
    source="NodeSpec.options['interpretation_requirements'] / SourceSpec.options['interpretation_requirements'], "
    "an untyped Mapping[str, Any] entry persisted on composer state and round-tripped through sessions.db "
    "storage, a composer LLM tool call, or YAML import",
    source_param="options",
    suppresses=("R5",),
    invariant="raises TypeError on a non-list value or a non-mapping list item; delegates per-item field "
    "validation to _coerce_requirement, itself a separately-declared boundary",
    test_ref="tests/unit/web/test_interpretation_state.py::test_requirements_rejects_non_list_value",
    test_fingerprint="c545bfa9b28f31378cf25bc76e022cd8d17239dedbf922fb4b36892b1e60a4c7",
)
def _requirements(options: Mapping[str, Any]) -> tuple[InterpretationRequirement, ...] | None:
    value = options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None
    if value is None:
        return None
    if not isinstance(value, (tuple, list)):
        raise TypeError("interpretation_requirements must be a list")
    requirements: list[InterpretationRequirement] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("interpretation requirement entries must be mappings")
        requirements.append(_coerce_requirement(item))
    return tuple(requirements)


def parse_interpretation_requirements(options: Mapping[str, Any]) -> tuple[InterpretationRequirement, ...] | None:
    """Public validated accessor for ``options.interpretation_requirements``.

    Returns ``None`` when the key is absent or null; otherwise every row is
    coerced through the same per-field validation this module applies before
    enumerating review sites (``KeyError`` / ``TypeError`` / ``ValueError``
    on any malformed row). External writers that need to read requirement
    rows they did not stage (e.g. the YAML import route surfacing pending
    review events, elspeth-ae5160c3cb) MUST come through here rather than
    hand-walking the raw list, so a row that this module would reject at the
    run gate can never be silently half-read upstream.
    """
    return _requirements(options)


@trust_boundary(
    tier=3,
    source="one interpretation_requirements list item: an untyped Mapping[str, Any] item extracted by "
    "_requirements from composer state (NodeSpec.options / SourceSpec.options) round-tripped through "
    "sessions.db storage, a composer LLM tool call, or YAML import",
    source_param="value",
    suppresses=("R5",),
    invariant="raises TypeError/ValueError on any malformed field (id, user_term, status, kind, "
    "accepted_value, accepted_artifact_hash, resolved_prompt_template_hash, draft, event_id) and "
    "constructs the owned InterpretationRequirement TypedDict only from validated fields; never "
    "substitutes a default for a present-but-malformed field",
    test_ref="tests/unit/web/test_interpretation_state.py::test_coerce_requirement_rejects_non_string_id",
    test_fingerprint="b1707a07d542960abbfdee200a95198c176e4132cd9e14fe0800bddb4298ae2b",
)
def _coerce_requirement(value: Mapping[str, Any]) -> InterpretationRequirement:
    requirement_id = value["id"]
    user_term = value["user_term"]
    status = value["status"]
    kind_value = value["kind"] if "kind" in value else InterpretationKind.VAGUE_TERM.value
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise TypeError("interpretation requirement id must be a non-empty string")
    if not isinstance(user_term, str) or not user_term.strip():
        raise TypeError("interpretation requirement user_term must be a non-empty string")
    if status not in ("pending", "resolved"):
        raise ValueError(f"unknown interpretation requirement status {status!r}")
    if not isinstance(kind_value, str):
        raise TypeError("interpretation requirement kind must be a string")
    try:
        kind = InterpretationKind(kind_value)
    except ValueError as exc:
        raise ValueError(f"unknown interpretation requirement kind {kind_value!r}") from exc
    accepted_value = value["accepted_value"] if "accepted_value" in value else None
    if status == "resolved" and not isinstance(accepted_value, str):
        raise TypeError("resolved interpretation requirement must carry accepted_value")
    accepted_artifact_hash = value["accepted_artifact_hash"] if "accepted_artifact_hash" in value else None
    if accepted_artifact_hash is not None and not isinstance(accepted_artifact_hash, str):
        raise TypeError("interpretation requirement accepted_artifact_hash must be a string or None")
    resolved_prompt_template_hash = value["resolved_prompt_template_hash"] if "resolved_prompt_template_hash" in value else None
    if resolved_prompt_template_hash is not None and not isinstance(resolved_prompt_template_hash, str):
        raise TypeError("interpretation requirement resolved_prompt_template_hash must be a string or None")
    draft = value["draft"] if "draft" in value else None
    if draft is not None and not isinstance(draft, str):
        raise TypeError("interpretation requirement draft must be a string or None")
    event_id = value["event_id"] if "event_id" in value else None
    if event_id is not None and not isinstance(event_id, str):
        raise TypeError("interpretation requirement event_id must be a string or None")
    return InterpretationRequirement(
        id=requirement_id,
        kind=kind.value,
        user_term=user_term,
        status=status,
        draft=draft,
        event_id=event_id,
        accepted_value=accepted_value,
        accepted_artifact_hash=accepted_artifact_hash,
        resolved_prompt_template_hash=resolved_prompt_template_hash,
    )


@trust_boundary(
    tier=3,
    source="SourceSpec.options['source_authoring'], an untyped Mapping[str, Any] persisted on the "
    "composer state and round-tripped through sessions.db storage or YAML import",
    source_param="options",
    suppresses=("R5",),
    invariant="raises TypeError on any malformed field (modality/content_hash/review_event_id/"
    "resolved_kind shape) and ValueError on an unknown resolved_kind enum value; never substitutes "
    "a default for a present-but-malformed field",
    test_ref="tests/unit/web/test_interpretation_state.py::test_source_authoring_metadata_rejects_non_string_modality",
    test_fingerprint="9c397610a6013437a0ba59a8e4f6fc8a53dfd24925a29b3f84e4f9b99b1b0f4e",
)
def _source_authoring_metadata(options: Mapping[str, Any]) -> SourceAuthoringMetadata | None:
    value = options[SOURCE_AUTHORING_KEY] if SOURCE_AUTHORING_KEY in options else None
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("source_authoring must be a mapping")
    modality = value["modality"]
    content_hash = value["content_hash"]
    review_event_id = value["review_event_id"] if "review_event_id" in value else None
    resolved_kind = value["resolved_kind"] if "resolved_kind" in value else None
    if not isinstance(modality, str) or not modality:
        raise TypeError("source_authoring.modality must be a non-empty string")
    if not isinstance(content_hash, str) or not content_hash:
        raise TypeError("source_authoring.content_hash must be a non-empty string")
    if review_event_id is not None and not isinstance(review_event_id, str):
        raise TypeError("source_authoring.review_event_id must be a string or None")
    if resolved_kind is not None:
        if not isinstance(resolved_kind, str):
            raise TypeError("source_authoring.resolved_kind must be a string or None")
        InterpretationKind(resolved_kind)
    return SourceAuthoringMetadata(
        modality=modality,
        content_hash=content_hash,
        review_event_id=review_event_id,
        resolved_kind=resolved_kind,
    )


def _is_llm_authored_modality(modality: str) -> bool:
    try:
        creation_modality = CreationModality(modality)
    except ValueError as exc:
        raise ValueError(f"unknown source authoring modality {modality!r}") from exc
    return creation_modality.requires_llm_provenance()


def _requirement_for_kind(
    requirements: Sequence[InterpretationRequirement] | None,
    kind: InterpretationKind,
) -> InterpretationRequirement | None:
    if requirements is None:
        return None
    matching = tuple(requirement for requirement in requirements if InterpretationKind(requirement["kind"]) is kind)
    if len(matching) > 1:
        raise ValueError(f"multiple interpretation requirements for kind {kind.value!r}")
    return matching[0] if matching else None


def _prompt_template_review_requirement(options: Mapping[str, Any]) -> InterpretationRequirement | None:
    return _requirement_for_kind(_requirements(options), InterpretationKind.LLM_PROMPT_TEMPLATE)


def _model_choice_review_requirement(options: Mapping[str, Any]) -> InterpretationRequirement | None:
    return _requirement_for_kind(_requirements(options), InterpretationKind.LLM_MODEL_CHOICE)


def _resolved_requirement_for_kind(
    requirements: Sequence[InterpretationRequirement] | None,
    kind: InterpretationKind,
) -> InterpretationRequirement | None:
    requirement = _requirement_for_kind(requirements, kind)
    if requirement is None or requirement["status"] != "resolved":
        return None
    return requirement


def _validate_prompt_template_review(node: NodeSpec, prompt_template: str | None) -> None:
    requirements = _requirements(node.options)
    resolved = _resolved_requirement_for_kind(requirements, InterpretationKind.LLM_PROMPT_TEMPLATE)
    if resolved is None:
        return
    # Structured nodes attest the prompt *skeleton*, not the substituted
    # text. The vague-term slot values are reviewed independently, so the
    # prompt-template review must be invariant under their resolution —
    # otherwise resolving a vague term (which rewrites the rendered prompt)
    # spuriously drifts a prompt-template review the operator already
    # approved. A genuine edit to a fixed text segment, or re-pointing a
    # slot to a different requirement, still changes the skeleton and drifts.
    # Multi-query nodes attest the whole prompt SURFACE (per-query templates,
    # system prompt, node-level template) — see
    # :func:`prompt_review_anchor_hash_from_options`.
    anchor_hash = prompt_review_anchor_hash_from_options(node.options)
    expected_hash = anchor_hash if anchor_hash is not None else stable_hash(prompt_template)
    stored_hash = resolved["resolved_prompt_template_hash"]
    if stored_hash != expected_hash:
        raise InterpretationReviewIntegrityError(
            f"llm node {node.id!r} prompt-template review hash drifted",
            component_id=node.id,
            component_type="transform",
            kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        )


def _validate_model_choice_review(node: NodeSpec, model: str) -> None:
    """Tier-1 read guard — resolved model choice must still match options.model.

    If a previously accepted ``llm_model_choice`` review exists but the
    node's current ``options.model`` no longer hashes to the same value,
    something changed the model after acceptance — that violates the
    "every model choice surfaced to the user" contract and the audit
    trail can no longer attribute the run's model to a user decision.
    Crash rather than silently let a drifted choice through.
    """
    requirements = _requirements(node.options)
    resolved = _resolved_requirement_for_kind(requirements, InterpretationKind.LLM_MODEL_CHOICE)
    if resolved is None:
        return
    expected_hash = model_choice_artifact_hash(model)
    if resolved["resolved_prompt_template_hash"] != expected_hash:
        raise InterpretationReviewIntegrityError(
            f"llm node {node.id!r} model-choice review hash drifted",
            component_id=node.id,
            component_type="transform",
            kind=InterpretationKind.LLM_MODEL_CHOICE,
        )


def pipeline_decision_artifact_hash(
    node: NodeSpec,
    all_nodes: Sequence[NodeSpec],
    *,
    user_term: str,
) -> str:
    """Canonical artifact hash for a pipeline-decision review.

    The hash domain is the *minimum* state projection that, if changed,
    would invalidate the prior review. Different decision kinds adjudicate
    different facts about the graph, so each one routes through its own
    projection helper. A whole-node hash would invalidate the review on
    unrelated edits (e.g. swapping the LLM model) — operationally noisy
    without auditability gain because the review's premise is unchanged.

    Both the write side (sessions/service when an interpretation-resolve
    event lands) and the read side (preflight materialisation) call this
    function so the hash is produced by exactly one piece of code.

    Adding a new pipeline-decision kind requires a registered helper —
    unknown user_terms raise rather than fall through to a permissive
    default.
    """

    normalized = user_term.strip()
    if normalized == PROMPT_SHIELD_USER_TERM:
        return _prompt_shield_artifact_hash(node, all_nodes)
    if normalized == RAW_HTML_CLEANUP_USER_TERM:
        return _raw_html_cleanup_artifact_hash(node, all_nodes)
    if normalized == WEB_SCRAPE_HTTP_IDENTITY_USER_TERM:
        return _web_scrape_http_identity_artifact_hash(node)
    if normalized == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM:
        return _required_control_auto_wired_artifact_hash(node)
    if normalized == GATE_CONDITION_AUTHORED_USER_TERM:
        return _gate_condition_authored_artifact_hash(node)
    raise ValueError(f"pipeline_decision_artifact_hash: unknown pipeline_decision user_term {user_term!r}")


def _gate_condition_authored_artifact_hash(node: NodeSpec) -> str:
    """Material-scoped hash for the planner-authored gate-semantics review.

    The review accepts a gate criterion the PLANNER chose rather than one the
    user stated verbatim. The hash binds to exactly the three fabrication axes
    the doctrine names:

    - the ``condition`` expression, which carries both the threshold VALUE and
      any category LITERAL compared against;
    - the ``routes`` mapping, whose every destination is material — an inverted
      route is the precise failure the "never invert stated routes" rule guards,
      and it changes no other field;
    - ``fork_to``, because a fork gate's route direction lives there rather than
      in ``routes``; omitting it would leave that axis unpinned for fork gates.

    ``options`` and ``on_error`` are deliberately excluded: neither changes the
    criterion the reviewer adjudicated, so editing them should leave an accepted
    review intact (the minimum-projection doctrine on this function).

    Route insertion order is NOT material — ``stable_hash`` canonicalises the
    mapping by key, so re-ordering the routes cannot drift an accepted review.
    """

    if node.node_type != "gate":
        raise ValueError(f"pipeline_decision_artifact_hash: gate_condition_authored requires a gate node, got {node.node_type!r}")
    return stable_hash(
        {
            "review_kind": "gate_condition_authored",
            "gate_node_id": node.id,
            "condition": node.condition,
            "routes": dict(node.routes) if node.routes is not None else None,
            "fork_to": list(node.fork_to) if node.fork_to is not None else None,
        }
    )


def _required_control_auto_wired_artifact_hash(node: NodeSpec) -> str:
    """Material-scoped hash for the auto-wired required-control disclosure.

    The review acknowledges that the server spliced this control node onto a
    specific edge because deployment policy requires the control. The hash
    binds to exactly that adjudication — the inserted node's identity, its
    plugin, and the edge it occupies (input and on_success). Re-pointing the
    node to a different edge or swapping the control implementation drifts the
    acknowledgement; unrelated option edits (thresholds, schema mode) do not
    change what was inserted where, so they leave the review intact.
    """

    if node.plugin is None:
        raise ValueError("pipeline_decision_artifact_hash: required_control_auto_wired requires a plugin-bearing node")
    return stable_hash(
        {
            "review_kind": "required_control_auto_wired",
            "node_id": node.id,
            "plugin": node.plugin,
            "input": node.input,
            "on_success": node.on_success,
        }
    )


@trust_boundary(
    tier=3,
    source="node.options, an untyped Mapping[str, Any] persisted on the composer node and round-tripped "
    "through sessions.db storage; only options.http is parsed here — node's other, typed fields are "
    "nominal ELSPETH-owned data, not part of this boundary",
    source_param="node",
    suppresses=("R5",),
    invariant="raises ValueError when options.http is present but not a Mapping, or when its "
    "abuse_contact/scraping_reason fields are missing or non-string; never substitutes a default for a "
    "present-but-malformed field",
    test_ref="tests/unit/web/test_interpretation_state.py::test_web_scrape_http_identity_artifact_hash_rejects_malformed_http_mapping",
    test_fingerprint="57fc3b74a14f0820f42cc7735a67a824da839a868c12c6ae960772b83678d154",
)
def _web_scrape_http_identity_artifact_hash(node: NodeSpec) -> str:
    """Material-scoped hash for the web_scrape HTTP identity review."""

    if node.plugin != "web_scrape":
        raise ValueError(f"pipeline_decision_artifact_hash: web_scrape_http_identity requires a web_scrape node, got {node.plugin!r}")
    http = node.options["http"] if "http" in node.options else None
    if not isinstance(http, Mapping):
        raise ValueError("pipeline_decision_artifact_hash: web_scrape_http_identity requires options.http")
    abuse_contact = http["abuse_contact"] if "abuse_contact" in http else None
    scraping_reason = http["scraping_reason"] if "scraping_reason" in http else None
    allowed_hosts = http["allowed_hosts"] if "allowed_hosts" in http else "public_only"
    if not isinstance(abuse_contact, str) or not abuse_contact.strip():
        raise ValueError("pipeline_decision_artifact_hash: web_scrape_http_identity requires http.abuse_contact")
    if not isinstance(scraping_reason, str) or not scraping_reason.strip():
        raise ValueError("pipeline_decision_artifact_hash: web_scrape_http_identity requires http.scraping_reason")
    return stable_hash(
        {
            "review_kind": "web_scrape_http_identity",
            "node_id": node.id,
            "abuse_contact": abuse_contact,
            "scraping_reason": scraping_reason,
            "allowed_hosts": allowed_hosts,
        }
    )


def _prompt_shield_artifact_hash(node: NodeSpec, all_nodes: Sequence[NodeSpec]) -> str:
    """Material-scoped hash for the prompt-shield recommendation review.

    The review accepts the recommendation that an authorized prompt-injection
    shield be inserted between an untrusted-content producer and this LLM.
    The hash binds to exactly that adjudication:

    - this LLM's node id (the review attaches to a specific node)
    - EVERY upstream path from this LLM's input back to either an authorized
      shield, an untrusted producer, or the end of the chain — each captured as
      a tuple of ``(producer_id, producer_plugin)`` pairs. A declared queue
      fans into every predecessor path, so changing either predecessor changes
      the hash. The paths are sorted, so predecessor insertion order does not.

    Fields like ``model``, ``temperature``, ``prompt_template``, ``api_key``
    and ``schema`` are intentionally NOT in scope: they don't change whether
    a shield is needed. Swapping the model after review should leave the
    review intact.
    """

    graph = _output_stream_graph(all_nodes)
    paths = _prompt_shield_upstream_paths(node.input, graph, frozenset())
    sorted_paths = sorted(paths, key=lambda path: tuple((pid, plugin or "") for pid, plugin in path))
    return stable_hash(
        {
            "review_kind": "prompt_shield_recommendation",
            "llm_node_id": node.id,
            "upstream_paths": [list(path) for path in sorted_paths],
        }
    )


_ShieldPath = tuple[tuple[str, str | None], ...]


def _prompt_shield_upstream_paths(stream: str | None, graph: _OutputStreamGraph, visited: frozenset[str]) -> list[_ShieldPath]:
    """Enumerate every upstream producer path from ``stream`` toward a shield/untrusted/end."""
    if not stream:
        return [()]
    producers = graph.producers_by_stream[stream] if stream in graph.producers_by_stream else ()
    if not producers:
        return [()]
    paths: list[_ShieldPath] = []
    for producer in producers:
        paths.extend(_prompt_shield_producer_paths(producer, graph, visited))
    return paths


def _prompt_shield_producer_paths(producer: NodeSpec, graph: _OutputStreamGraph, visited: frozenset[str]) -> list[_ShieldPath]:
    head: tuple[str, str | None] = (producer.id, producer.plugin)
    if producer.id in visited:
        return [(head,)]  # cycle — stop, still record this producer
    visited = visited | {producer.id}
    if _is_effective_prompt_shield(producer) or producer.plugin in untrusted_content_transform_names():
        return [(head,)]  # adjudication boundary reached
    if producer.node_type == "queue":
        predecessors = graph.queue_predecessors[producer.id] if producer.id in graph.queue_predecessors else ()
        if not predecessors:
            return [(head,)]
        return [(head, *sub) for predecessor in predecessors for sub in _prompt_shield_producer_paths(predecessor, graph, visited)]
    if producer.node_type == "row_union":
        branches = _coalesce_branch_connections(producer.branches)
        if not branches:
            return [(head,)]
        return [(head, *sub) for branch in branches for sub in _prompt_shield_upstream_paths(branch, graph, visited)]
    return [(head, *sub) for sub in _prompt_shield_upstream_paths(producer.input, graph, visited)]


@trust_boundary(
    tier=3,
    source="node.options['mapping'], an untyped Mapping[str, Any] persisted on the composer node and "
    "round-tripped through sessions.db storage",
    source_param="node",
    suppresses=("R5",),
    invariant="raises ValueError when options['mapping'] is present but not a Mapping; an absent key "
    "defaults to {} (matching field_mapper's own FieldMapperConfig.mapping default_factory=dict), so "
    "only genuine absence — never a present-but-malformed shape — is treated as empty",
    test_ref="tests/unit/web/test_interpretation_state.py::test_raw_html_cleanup_artifact_hash_rejects_malformed_mapping_shape",
    test_fingerprint="92b3cd5feff4db8445cc652f50da44578206ac25d813b479c601ad314d9fb908",
)
def _raw_html_cleanup_artifact_hash(node: NodeSpec, all_nodes: Sequence[NodeSpec]) -> str:
    """Material-scoped hash for the raw-html cleanup review.

    The review accepts that this field_mapper drops the upstream web_scrape's
    raw content/fingerprint fields. The hash binds to:

    - this field_mapper's node id
    - its ``mapping`` and ``select_only`` flag (the topological adjudication)
    - the set of raw fields any upstream web_scrape exposes (so adding a new
      raw field upstream re-stages the review — that's a material change to
      what "drop the raw fields" means).

    ``schema.guaranteed_fields`` on the field_mapper itself is excluded —
    it's a downstream consequence of the mapping, not adjudication input.
    """

    upstream_raw_fields = sorted(_web_scrape_raw_fields(all_nodes))
    raw_mapping = node.options["mapping"] if "mapping" in node.options else None
    if raw_mapping is not None and not isinstance(raw_mapping, Mapping):
        raise ValueError(
            f"pipeline_decision_artifact_hash: raw_html_cleanup requires field_mapper.mapping to be a "
            f"mapping on node {node.id!r}, got {type(raw_mapping).__name__}"
        )
    mapping: dict[str, Any] = dict(raw_mapping) if raw_mapping is not None else {}
    select_only = "select_only" in node.options and node.options["select_only"] is True
    return stable_hash(
        {
            "review_kind": "raw_html_cleanup",
            "field_mapper_node_id": node.id,
            "mapping": mapping,
            "select_only": select_only,
            "upstream_web_scrape_raw_fields": upstream_raw_fields,
        }
    )


def _validate_pipeline_decision_review(node: NodeSpec, all_nodes: Sequence[NodeSpec]) -> None:
    requirements = _requirements(node.options)
    resolved = _resolved_requirement_for_kind(requirements, InterpretationKind.PIPELINE_DECISION)
    if resolved is None:
        return
    # On a RESOLVED row, a node that no longer implements the approved decision
    # (or no longer yields its artifact at all) is drift of that review, the
    # same as a changed artifact hash. Both shared helpers raise a plain
    # ValueError for their staging-time callers, so the refusal is typed here,
    # message bytes unchanged.
    try:
        validate_pipeline_decision_node_semantics(
            node=node,
            all_nodes=all_nodes,
            user_term=resolved["user_term"],
            draft=resolved["draft"],
            context="interpretation_state",
        )
        expected_hash = pipeline_decision_artifact_hash(node, all_nodes, user_term=resolved["user_term"])
    except ValueError as exc:
        raise InterpretationReviewIntegrityError(
            str(exc),
            component_id=node.id,
            component_type="transform",
            kind=InterpretationKind.PIPELINE_DECISION,
        ) from exc
    if resolved["accepted_artifact_hash"] != expected_hash:
        raise InterpretationReviewIntegrityError(
            f"node {node.id!r} pipeline-decision review hash drifted",
            component_id=node.id,
            component_type="transform",
            kind=InterpretationKind.PIPELINE_DECISION,
        )


@trust_boundary(
    tier=3,
    source="NodeSpec.options['prompt_template_parts'], an untyped Mapping[str, Any] entry persisted on "
    "the composer node and round-tripped through sessions.db storage or a composer LLM tool call",
    source_param="options",
    suppresses=("R5",),
    invariant="raises TypeError/ValueError on any malformed part shape (non-list value, non-mapping "
    "item, unknown/missing kind, non-string text or requirement_id); never substitutes a default for a "
    "present-but-malformed field",
    test_ref="tests/unit/web/test_interpretation_state.py::test_prompt_parts_rejects_non_list_value",
    test_fingerprint="f336e86a56c756420ac391c350cbf8c1e9a768284669286d103a4575dcefcb2f",
)
def _prompt_parts(options: Mapping[str, Any]) -> tuple[PromptPart, ...] | None:
    value = options[PROMPT_TEMPLATE_PARTS_KEY] if PROMPT_TEMPLATE_PARTS_KEY in options else None
    if value is None:
        return None
    if not isinstance(value, (tuple, list)):
        raise TypeError("prompt_template_parts must be a list")
    parts: list[PromptPart] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("prompt_template_parts entries must be mappings")
        kind = item["kind"]
        if kind == "text":
            text = item["text"]
            if not isinstance(text, str):
                raise TypeError("text prompt part requires string text")
            parts.append(PromptPart(kind="text", text=text))
        elif kind == "interpretation_ref":
            requirement_id = item["requirement_id"]
            if not isinstance(requirement_id, str) or not requirement_id:
                raise TypeError("interpretation_ref prompt part requires requirement_id")
            parts.append(PromptPart(kind="interpretation_ref", requirement_id=requirement_id))
        else:
            raise ValueError(f"unknown prompt_template_parts kind {kind!r}")
    return tuple(parts)


def prompt_structure_hash(parts: tuple[PromptPart, ...]) -> str:
    """Canonical hash of a prompt's *structure* — text segments and the
    requirement each interpretation slot references — independent of whether
    those slots are pending or resolved and of their accepted values.

    This is the attestation domain for the ``llm_prompt_template`` review: that
    review approves the LLM-authored prompt skeleton. The vague-term reviews
    approve the slot *values* separately. Anchoring the prompt-template review
    to this skeleton makes it invariant under interpretation resolution — so
    resolving a vague term (which rewrites the rendered ``prompt_template``)
    does not drift the prompt-template review, while a genuine edit to a fixed
    text segment or a re-pointed slot still does.

    The projection deliberately excludes requirement status and accepted_value:
    those belong to the vague-term reviews' own attestations.
    """
    skeleton: list[tuple[str, str]] = []
    for part in parts:
        kind = part["kind"]
        if kind == "text":
            skeleton.append(("text", part["text"]))
        elif kind == "interpretation_ref":
            skeleton.append(("interpretation_ref", part["requirement_id"]))
        else:
            raise ValueError(f"unknown prompt part kind {kind!r}")
    return stable_hash(skeleton)


def prompt_structure_hash_from_options(options: Mapping[str, Any]) -> str | None:
    """Skeleton hash for a node's prompt parts, or ``None`` for a legacy
    (no-parts) node. Single derivation shared by the resolve-time anchor and the
    execution-time drift guard so they cannot diverge."""
    parts = _prompt_parts(options)
    if parts is None:
        return None
    return prompt_structure_hash(parts)


# Attestation-domain tag hashed into every multi-query prompt-surface anchor so
# the anchor can never collide with a single-prompt ``stable_hash(prompt_template)``
# or a ``prompt_structure_hash`` skeleton, and so a future widening of the
# surface is a visible domain bump rather than a silent redefinition.
PROMPT_SURFACE_HASH_DOMAIN: Final[str] = "llm_prompt_surface/v1"
# The ``llm_draft`` wire field is bounded (``sessions/schemas.py``
# ``InterpretationEventResponse.llm_draft``: 8192 chars). Nothing on the write
# path checks it — the pending event row is a ``Text`` column — so an
# over-long draft is stored and then fails the READ projection, taking down
# ``GET /interpretations`` for the whole session. The review render therefore
# stays under this bound by per-text shortening and, when that cannot fit,
# whole-entry omission (``MultiQueryPromptSurface.render_for_review``). The
# ANCHOR always covers the complete texts — only the display is
# bounded, and every shortened or omitted template says so inline.
PROMPT_SURFACE_REVIEW_MAX_CHARS: Final[int] = 8000
# Inline marker that stands in for the cut tail of a shortened review text
# (``{cut}`` = how many chars are not shown). Shared by the multi-query
# per-text shortening and the single-prompt draft bound so every shortened
# review text reads the same way.
_PROMPT_REVIEW_SHORTENED_MARKER: Final[str] = " […{cut} more chars not shown; the review attests the full text]"
_PROMPT_SURFACE_TEMPLATE_DISPLAY_MIN_CHARS: Final[int] = 200
_PROMPT_SURFACE_NODE_TEMPLATE_USERS_PREFIX: Final[str] = "Node-level prompt_template, used by queries without their own template: "
_PROMPT_SURFACE_NODE_TEMPLATE_UNUSED_LINE: Final[str] = "Node-level prompt_template: not used (every query supplies its own template)."
_PROMPT_SURFACE_NODE_TEMPLATE_UNUSED_WITH_INVALID_LINE: Final[str] = "Node-level prompt_template: not used (no query falls back to it)."
_PROMPT_SURFACE_INVALID_QUERY_TEMPLATE_TEXT: Final[str] = "(template value is not text; plugin validation rejects this node)"


@dataclass(frozen=True, slots=True)
class InvalidQueryTemplate:
    """A query whose ``template`` is present but is not a string.

    Neither a template (nothing renders as prose) nor a fallback to the
    node-level ``prompt_template`` (the plugin's ``QueryDefinition`` schema
    rejects the node before any query runs). Carried as its own owned value in
    :attr:`MultiQueryPromptSurface.queries` so it can never be confused with a
    real template text or with the ``None`` fallback.
    """

    reason: Literal["not_text"] = "not_text"


@dataclass(frozen=True, slots=True)
class MultiQueryPromptSurface:
    """The complete prompt surface a multi-query LLM node sends the model.

    Session 94f6f00c (2026-09-13): the ``llm_prompt_template`` review card and
    its anchor covered only the node-level ``prompt_template``. On a
    multi-query node every query may carry its own ``template`` override, in
    which case the node-level template never renders — the operator attested
    dead text while the per-query templates and the shared ``system_prompt``,
    the prompts that actually run, were never reviewed. This value is what the
    review approves for that node shape: the node-level template (with its
    skeleton when structured), the system prompt, and each query's override
    in authoring order: a ``str`` template, ``None`` (falls back to the
    node-level template), or :class:`InvalidQueryTemplate` (present but not
    text — neither rendered nor a node-level-template user).
    """

    node_prompt_template: str | None
    node_prompt_structure_hash: str | None
    system_prompt: str | None
    queries: tuple[tuple[str, str | InvalidQueryTemplate | None], ...]

    @property
    def node_template_users(self) -> tuple[str, ...]:
        return tuple(name for name, override in self.queries if override is None)

    @property
    def _templated_count(self) -> int:
        """How many queries carry their own string template."""
        return sum(1 for _name, override in self.queries if isinstance(override, str))

    @property
    def _has_invalid_templates(self) -> bool:
        return any(isinstance(override, InvalidQueryTemplate) for _name, override in self.queries)

    def _node_template_unused_line(self) -> str:
        if self._has_invalid_templates:
            return _PROMPT_SURFACE_NODE_TEMPLATE_UNUSED_WITH_INVALID_LINE
        return _PROMPT_SURFACE_NODE_TEMPLATE_UNUSED_LINE

    def anchor_hash(self) -> str:
        """Requirement-level attestation anchor over the whole surface.

        The node-level template contributes its skeleton hash when structured
        (invariant under vague-term resolution, like the single-prompt anchor)
        and its text otherwise. A query's string template or ``None`` fallback
        is hashed as itself (so every well-formed surface keeps its anchor);
        an :class:`InvalidQueryTemplate` is hashed as a mapping, which no
        string or ``None`` can equal, so repairing it moves the anchor.
        """
        return stable_hash(
            {
                "domain": PROMPT_SURFACE_HASH_DOMAIN,
                "node_prompt": self.node_prompt_structure_hash
                if self.node_prompt_structure_hash is not None
                else stable_hash(self.node_prompt_template),
                "system_prompt": self.system_prompt,
                "queries": [
                    [name, {"invalid_template": override.reason} if isinstance(override, InvalidQueryTemplate) else override]
                    for name, override in self.queries
                ],
            }
        )

    def render_for_review(self, *, max_chars: int = PROMPT_SURFACE_REVIEW_MAX_CHARS) -> str:
        """Human-readable draft of the surface for the review card.

        Fact-register labels ("not used" / "used by"), never editorial: the
        operator is approving what the model receives, not grading composer
        hygiene. The anchor is never bounded; only this display is, by two
        mechanisms applied in order:

        1. **Per-text shortening** (:meth:`_bounded_texts`): the longest texts
           are halved first, each with an inline marker, down to a floor of
           about 200 kept chars plus the marker. The floor is a terminal state,
           so this always terminates, but it cannot fit every surface: many
           queries each at the floor, or long query names, still exceed
           ``max_chars``.
        2. **Whole-entry omission** (:meth:`_render_with_whole_entry_omissions`),
           engaged ONLY when the mechanism-1 render is longer than
           ``max_chars`` (so every draft that fits after mechanism 1 is
           unchanged): query templates are replaced from the end by one
           fact-register line each, then — if that is still too long —
           trailing queries stop being listed; count lines say how many of
           each. If even that does not fit, the most compact form (no query
           listed) is returned: only the framing, the count lines and the
           system prompt and node-level template, each either within the
           mechanism-1 budget or at its floor. That form fits the default
           ``max_chars``; a caller passing a very small ``max_chars`` (under
           about a thousand chars) gets no bound.
        """
        texts: list[str | None] = [
            self.system_prompt,
            *(override if isinstance(override, str) else None for _name, override in self.queries),
        ]
        if self.node_template_users:
            texts.append(self.node_prompt_template)
        budget = max_chars - self._frame_chars()
        display = self._bounded_texts(texts, budget)
        rendered = "\n".join(self._review_lines(display, omitted=frozenset(), listed=len(self.queries)))
        if len(rendered) <= max_chars:
            return rendered
        return self._render_with_whole_entry_omissions(display, max_chars)

    def _review_lines(self, display: list[str | None], *, omitted: frozenset[int], listed: int) -> list[str]:
        """The draft's lines with the queries at ``omitted`` shown as fact lines
        and only the first ``listed`` queries listed. ``omitted=frozenset()``
        with every query listed is the mechanism-1 draft."""
        lines = self._head_lines(display)
        for position in range(listed):
            lines.extend(self._query_lines(display, position, omitted=position in omitted))
        listed_users = sum(1 for _name, override in self.queries[:listed] if override is None)
        lines.extend(self._tail_lines(display, omitted_count=len(omitted), listed=listed, listed_users=listed_users))
        return lines

    @staticmethod
    def _head_lines(display: list[str | None]) -> list[str]:
        system_display = display[0]
        return [
            "Multi-query LLM node: for every row the model receives one call per query below.",
            "",
            "System prompt (sent with every query):",
            system_display if system_display is not None else "(none)",
        ]

    def _query_lines(self, display: list[str | None], position: int, *, omitted: bool) -> list[str]:
        name, override = self.queries[position]
        if override is None:
            return ["", f"Query '{name}': uses the node-level prompt_template (below)."]
        if isinstance(override, InvalidQueryTemplate):
            return ["", f"Query '{name}': {_PROMPT_SURFACE_INVALID_QUERY_TEMPLATE_TEXT}"]
        if omitted:
            return ["", f"Query '{name}': (template of {len(override)} chars not shown; the review attests the full text)"]
        return ["", f"Query '{name}':", display[position + 1] or ""]

    def _tail_lines(self, display: list[str | None], *, omitted_count: int, listed: int, listed_users: int) -> list[str]:
        """Count lines (only when something is omitted or unlisted) and the
        node-level template section. ``listed_users`` is how many of
        :attr:`node_template_users` fall among the first ``listed`` queries."""
        lines: list[str] = []
        users = self.node_template_users
        count_lines = self._count_lines(omitted_count=omitted_count, templated=self._templated_count, listed=listed)
        if count_lines:
            lines.append("")
            lines.extend(count_lines)
        lines.append("")
        if users:
            names = ", ".join(users[:listed_users]) + self._unlisted_users_suffix(
                unlisted=len(users) - listed_users, any_listed=listed_users > 0
            )
            lines.append(_PROMPT_SURFACE_NODE_TEMPLATE_USERS_PREFIX + names)
            lines.append(display[len(self.queries) + 1] or "")
        else:
            lines.append(self._node_template_unused_line())
        return lines

    def _count_lines(self, *, omitted_count: int, templated: int, listed: int) -> list[str]:
        """``templated`` is how many queries carry their own template (passed in
        so the per-step mechanism-2 sizing stays O(1))."""
        lines: list[str] = []
        if omitted_count:
            lines.append(
                f"Query templates not shown in this draft: {omitted_count} of {templated}; the review attests every template in full."
            )
        unlisted = len(self.queries) - listed
        if unlisted:
            lines.append(
                f"Queries not listed in this draft: the last {unlisted} of {len(self.queries)}; "
                "the review attests their names and templates in full."
            )
        return lines

    @staticmethod
    def _unlisted_users_suffix(*, unlisted: int, any_listed: bool) -> str:
        if not unlisted:
            return ""
        return f", and {unlisted} more not listed" if any_listed else f"{unlisted} not listed"

    def _render_with_whole_entry_omissions(self, display: list[str | None], max_chars: int) -> str:
        """Mechanism 2 of :meth:`render_for_review`, for a mechanism-1 draft longer than ``max_chars``.

        Stage 1 replaces query templates from the END with one fact line each
        (only where that line is shorter than the template it replaces);
        stage 2 then stops listing queries from the end. Each step is sized
        incrementally and the first step estimated to fit is rendered and
        measured, so an estimate can only cost a further step, never an
        over-long draft. Bounded by ``2 * len(queries)`` steps; returns the
        most compact form (no query listed) when no step fits.
        """
        count = len(self.queries)
        users = self.node_template_users
        head_chars = len("\n".join(self._head_lines(display)))
        full_chars = [_appended_lines_chars(self._query_lines(display, position, omitted=False)) for position in range(count)]
        omitted_chars = [_appended_lines_chars(self._query_lines(display, position, omitted=True)) for position in range(count)]
        node_display_chars = len(display[count + 1] or "") if users else 0
        omitted: set[int] = set()
        listed = count
        listed_users = len(users)
        listed_user_name_chars = sum(len(name) for name in users)
        query_chars = sum(full_chars)
        # Every whole-surface property scans ``queries``; read each once here so
        # the up-to-``2 * count`` sizing steps below stay O(1) each.
        templated = self._templated_count
        unused_line_chars = len(self._node_template_unused_line())

        def estimated_tail_chars() -> int:
            tail_chars = sum(len(line) + 1 for line in self._count_lines(omitted_count=len(omitted), templated=templated, listed=listed))
            if tail_chars:
                tail_chars += 1
            if users:
                joined = listed_user_name_chars + 2 * (listed_users - 1) if listed_users else 0
                suffix = self._unlisted_users_suffix(unlisted=len(users) - listed_users, any_listed=listed_users > 0)
                return tail_chars + 1 + len(_PROMPT_SURFACE_NODE_TEMPLATE_USERS_PREFIX) + joined + len(suffix) + 1 + node_display_chars + 1
            return tail_chars + 1 + unused_line_chars + 1

        def measured_fit() -> str | None:
            if head_chars + query_chars + estimated_tail_chars() > max_chars:
                return None
            rendered = "\n".join(self._review_lines(display, omitted=frozenset(omitted), listed=listed))
            return rendered if len(rendered) <= max_chars else None

        for position in reversed(range(count)):
            if not isinstance(self.queries[position][1], str) or omitted_chars[position] >= full_chars[position]:
                continue
            omitted.add(position)
            query_chars += omitted_chars[position] - full_chars[position]
            fitted = measured_fit()
            if fitted is not None:
                return fitted
        for position in reversed(range(count)):
            name, override = self.queries[position]
            query_chars -= omitted_chars[position] if position in omitted else full_chars[position]
            omitted.discard(position)
            listed = position
            if override is None:
                listed_users -= 1
                listed_user_name_chars -= len(name)
            fitted = measured_fit()
            if fitted is not None:
                return fitted
        return "\n".join(self._review_lines(display, omitted=frozenset(), listed=0))

    def _frame_chars(self) -> int:
        # Estimate of the fixed framing text, used only to size mechanism 1's
        # template budget. It is NOT an upper bound (a long query name on a
        # query that falls back to the node-level template appears twice), so
        # render_for_review measures the actual draft before returning it and
        # engages whole-entry omission when it is still too long.
        return 160 + sum(len(name) + 64 for name, _override in self.queries) + 160

    @staticmethod
    def _bounded_texts(texts: list[str | None], budget: int) -> list[str | None]:
        """Shorten the longest texts first until the total fits ``budget`` or nothing can shrink.

        Each step replaces the longest shrinkable text with the first half of
        its ORIGINAL (at least ``_PROMPT_SURFACE_TEMPLATE_DISPLAY_MIN_CHARS``
        chars) plus an inline marker. A text whose replacement would not be
        strictly shorter than what it currently shows is at its floor: it is
        exhausted and never selected again, and the loop stops when no
        shrinkable text remains. The total can therefore still exceed
        ``budget`` on return; :meth:`render_for_review` handles that with
        whole-entry omission. Every step either strictly shortens a text or
        exhausts one, so the loop terminates for every input.

        Selection is longest first, lowest index on a tie. A heap keyed
        ``(-length, index)`` and a running total keep that choice while
        making the loop ``O(n log n)`` rather than rescanning every text per
        step. The heap holds exactly one entry per text that is still
        selectable: a popped text is pushed back only with its new length.
        """
        current = list(texts)
        total = sum(len(text) for text in current if text is not None)
        heap = [(-len(text), index) for index, text in enumerate(current) if text is not None]
        heapq.heapify(heap)
        marker = _PROMPT_REVIEW_SHORTENED_MARKER
        while total > budget and heap:
            _negative_length, longest = heapq.heappop(heap)
            text = current[longest] or ""
            if len(text) <= _PROMPT_SURFACE_TEMPLATE_DISPLAY_MIN_CHARS:
                break
            keep = max(_PROMPT_SURFACE_TEMPLATE_DISPLAY_MIN_CHARS, len(text) // 2)
            original = texts[longest] or ""
            replacement = original[:keep] + marker.format(cut=len(original) - keep)
            if len(replacement) >= len(text):
                # At its floor: exhausted, and not pushed back, so never selected again.
                continue
            current[longest] = replacement
            total -= len(text) - len(replacement)
            heapq.heappush(heap, (-len(replacement), longest))
        return current


def _appended_lines_chars(lines: list[str]) -> int:
    """Chars ``lines`` add to a ``"\\n".join`` that already has a line before them."""
    return sum(len(line) + 1 for line in lines)


@observation_boundary(
    tier=3,
    source="web-authored llm node options mapping (untrusted prompt_template, system_prompt and queries values)",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant=(
        "returns None unless a string prompt_template sits beside a queries value with at least one "
        "well-formed entry; otherwise constructs an owned MultiQueryPromptSurface from string values only, "
        "reading a non-string system_prompt as absent, an absent query template as a fallback to the "
        "node-level template, and a present non-string query template as an InvalidQueryTemplate that "
        "is neither a template nor a fallback; raises only the TypeError, ValueError or KeyError that "
        "prompt_structure_hash_from_options raises on malformed prompt_template_parts"
    ),
)
def multi_query_prompt_surface_from_options(options: Mapping[str, Any]) -> MultiQueryPromptSurface | None:
    """The prompt surface of a multi-query LLM node, or ``None`` for any other shape.

    Multi-query means: a string ``prompt_template`` beside a ``queries`` option
    with at least one well-formed entry (:func:`_well_formed_query_entries`,
    the composer's single reading of the mapping/list forms). Each query's
    ``template`` is read three ways: a string is its override; an absent (or
    ``None``) template falls back to the node-level template; a present but
    non-string template is :class:`InvalidQueryTemplate` — not rendered and not
    a node-level-template user, because plugin schema validation rejects the
    node. That matches the binding guard
    (``state._validate_multi_query_template_variable_bindings``), which skips
    such a query whole without counting it as a node-level-template user, and
    the advisor evidence walk (``service._advisor_query_option_values``).
    Malformed ``prompt_template_parts`` propagate the TypeError, ValueError
    or KeyError of :func:`prompt_structure_hash_from_options`, exactly as on a
    single-prompt node; every other shape never raises.
    """
    raw_queries = options["queries"] if "queries" in options else None
    prompt_template = options["prompt_template"] if "prompt_template" in options else None
    if raw_queries is None:
        return None
    entries = _well_formed_query_entries(raw_queries)
    if not entries:
        return None
    if prompt_template is not None and not isinstance(prompt_template, str):
        marker = is_widened_blob_ref(deep_thaw(prompt_template))
        if (
            marker is None
            or marker.mode != "inline_content"
            or any("template" not in entry or entry["template"] is None for _, entry in entries)
        ):
            return None
        prompt_template = None
    raw_system_prompt = options["system_prompt"] if "system_prompt" in options else None
    queries: list[tuple[str, str | InvalidQueryTemplate | None]] = []
    for name, entry in entries:
        override = entry["template"] if "template" in entry else None
        if override is None or isinstance(override, str):
            queries.append((name, override))
        else:
            queries.append((name, InvalidQueryTemplate()))
    return MultiQueryPromptSurface(
        node_prompt_template=prompt_template,
        node_prompt_structure_hash=prompt_structure_hash_from_options(options),
        system_prompt=raw_system_prompt if isinstance(raw_system_prompt, str) else None,
        queries=tuple(queries),
    )


def prompt_review_anchor_hash_from_options(options: Mapping[str, Any]) -> str | None:
    """Requirement-level anchor for the ``llm_prompt_template`` review.

    ONE derivation for every site that writes or checks that anchor — the
    review artifact, the pending-event domain, the resolve-time surfacing/live
    comparison, and the execution-time drift guard — so they cannot diverge:

    * multi-query node → :meth:`MultiQueryPromptSurface.anchor_hash` over the
      whole prompt surface;
    * structured single-prompt node → the ``prompt_structure_hash`` skeleton;
    * unstructured single-prompt node → ``None``; callers use
      ``stable_hash(prompt_template)``.
    """
    surface = multi_query_prompt_surface_from_options(options)
    if surface is not None:
        return surface.anchor_hash()
    structure = prompt_structure_hash_from_options(options)
    system = options["system_prompt"] if "system_prompt" in options else None
    if system is None:
        return structure
    prompt = options["prompt_template"] if "prompt_template" in options else None
    return stable_hash(
        {
            "domain": "elspeth.single-prompt-review.v1",
            "node_prompt": structure if structure is not None else stable_hash(prompt),
            "system_prompt": system,
        }
    )


@trust_boundary(
    tier=3,
    source="Untrusted LLM-authored or YAML-imported prompt/query/system values in node options",
    source_param="options",
    suppresses=("R5",),
    invariant="rejects malformed non-blob prompt/system/query templates with ValueError; returns None for valid unresolved inline blob sources, otherwise hashes effective text",
    test_ref="tests/unit/web/test_prompt_artifact_boundary.py::test_prompt_artifact_rejects_malformed_options",
    test_fingerprint="8fa66b482b95e4f28e3a87d6a002972ad57a000eadbd54b167f5d793a436cbbf",
)
def approved_prompt_artifact_hash_from_options(options: Mapping[str, Any]) -> str | None:
    """Hash resolved text, or return no approval link for unresolved blob sources.

    Uploaded content is verified through blob identity, hash and resolution
    records. It does not acquire full-text approval merely by substituting
    after a review. Per-site review evidence remains independently enforced.
    """
    prompt = options["prompt_template"] if "prompt_template" in options else None
    system = options["system_prompt"] if "system_prompt" in options else None
    surface = multi_query_prompt_surface_from_options(options)
    if surface is not None and not surface.node_template_users:
        prompt = None
    if (prompt is not None and not isinstance(prompt, str) and not _is_inline_prompt_blob(prompt)) or (
        system is not None and not isinstance(system, str) and not _is_inline_prompt_blob(system)
    ):
        raise ValueError("Approved prompt artifact requires text prompt and system templates")
    entries = _well_formed_query_entries(options["queries"]) if "queries" in options else ()
    if "queries" in options:
        raw_queries = options["queries"]
        if raw_queries is not None and not entries and not _is_inline_prompt_blob(raw_queries):
            raise ValueError("Approved prompt artifact requires query definitions")
        for name, entry in entries:
            template = entry["template"] if "template" in entry else None
            if template is not None and not isinstance(template, str) and not _is_inline_prompt_blob(template):
                raise ValueError(f"Query {name!r} template is not text or an inline blob source")
    values = [options[key] for key in ("system_prompt", "queries") if key in options]
    uses_fallback = not entries or any("template" not in query or query["template"] is None for _name, query in entries)
    if uses_fallback and "prompt_template" in options:
        values.append(options["prompt_template"])
    for _name, query in entries:
        values.append(query)
        if "template" in query:
            values.append(query["template"])
    if any(_is_inline_prompt_blob(value) for value in values):
        return None
    if surface is not None:
        queries: list[tuple[str, str | None]] = []
        for name, template in surface.queries:
            if isinstance(template, InvalidQueryTemplate):
                raise ValueError(f"Query {name!r} template is not text")
            queries.append((name, template))
        return approved_prompt_artifact_hash(
            prompt_template=surface.node_prompt_template, system_prompt=surface.system_prompt, queries=tuple(queries)
        )
    if prompt is None:
        raise ValueError("Approved prompt artifact requires an effective prompt template")
    return approved_prompt_artifact_hash(prompt_template=prompt, system_prompt=system)


@observation_boundary(
    tier=3,
    source="web-authored llm node options mapping (untrusted prompt_template value)",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant=(
        "returns the rendered multi-query prompt surface when the options describe one, the string "
        "prompt_template otherwise (unchanged at or under PROMPT_SURFACE_REVIEW_MAX_CHARS, else its head "
        "plus an inline shortened-text marker within that bound), and None when prompt_template is absent "
        "or not a string; never raises"
    ),
)
def prompt_review_draft_from_options(options: Mapping[str, Any]) -> str | None:
    """The text the operator reviews for an LLM node's prompt-template review.

    Multi-query nodes review the rendered prompt surface; every other shape
    keeps reviewing ``prompt_template`` itself. ``None`` when the node carries
    no string ``prompt_template`` (nothing to review yet).

    Both shapes stay within ``PROMPT_SURFACE_REVIEW_MAX_CHARS`` so the draft
    always fits the bounded ``llm_draft`` wire field. A single-prompt template
    at or under the bound is returned byte-for-byte; a longer one keeps its
    head and says inline how many chars are not shown. Only the display is
    bounded: the review anchor and the node-level
    ``resolved_prompt_template_hash`` cover the complete template.
    """
    surface = multi_query_prompt_surface_from_options(options)
    if surface is not None:
        return surface.render_for_review()
    prompt_template = options["prompt_template"] if "prompt_template" in options else None
    if not isinstance(prompt_template, str):
        return None
    system_prompt = options["system_prompt"] if "system_prompt" in options else None
    if isinstance(system_prompt, str):
        frame = "System prompt:\n\n\nPrompt template:\n"
        section_budget = (PROMPT_SURFACE_REVIEW_MAX_CHARS - len(frame)) // 2
        system_display = _bounded_single_prompt_review_draft(system_prompt, max_chars=section_budget)
        prompt_display = _bounded_single_prompt_review_draft(prompt_template, max_chars=section_budget)
        return f"System prompt:\n{system_display}\n\nPrompt template:\n{prompt_display}"
    return _bounded_single_prompt_review_draft(prompt_template)


def _bounded_single_prompt_review_draft(prompt_template: str, *, max_chars: int = PROMPT_SURFACE_REVIEW_MAX_CHARS) -> str:
    if len(prompt_template) <= max_chars:
        return prompt_template
    # Size the marker with the whole length: the real cut is smaller, so its
    # digit count can only be equal or fewer and the result stays in bounds.
    keep = max_chars - len(_PROMPT_REVIEW_SHORTENED_MARKER.format(cut=len(prompt_template)))
    return prompt_template[:keep] + _PROMPT_REVIEW_SHORTENED_MARKER.format(cut=len(prompt_template) - keep)


def _render_prompt_parts(
    parts: tuple[PromptPart, ...],
    requirements_by_id: Mapping[str, InterpretationRequirement],
    *,
    unresolved_text: str | None,
) -> str:
    rendered: list[str] = []
    for part in parts:
        kind = part["kind"]
        if kind == "text":
            rendered.append(part["text"])
            continue
        if kind != "interpretation_ref":
            raise ValueError(f"unknown prompt part kind {kind!r}")
        requirement_id = part["requirement_id"]
        if requirement_id not in requirements_by_id:
            raise ValueError(f"prompt part references unknown interpretation requirement {requirement_id!r}")
        requirement = requirements_by_id[requirement_id]
        if requirement["status"] == "pending":
            if unresolved_text is None:
                raise ValueError(f"interpretation requirement {requirement_id!r} is still pending")
            rendered.append(unresolved_text)
            continue
        accepted = requirement["accepted_value"]
        if accepted is None:
            raise TypeError(f"resolved interpretation requirement {requirement_id!r} has no accepted value")
        rendered.append(accepted)
    return "".join(rendered)


def _legacy_terms(prompt_template: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in INTERPRETATION_PLACEHOLDER_RE.finditer(prompt_template))


@trust_boundary(
    tier=3,
    source="NodeSpec.options['interpretation_requirements'] / ['prompt_template_parts'] / "
    "['prompt_template'], untyped Mapping[str, Any] entries persisted on composer state and "
    "round-tripped through sessions.db storage",
    source_param="options",
    suppresses=("R5",),
    invariant="returns 0 for any malformed sub-shape rather than raising (the Tier-3 staging idiom "
    "documented in this function's own docstring); every caller treats a result != 1 as unresolvable "
    "and rejects/blocks the mutation rather than accepting it, so the sentinel is fail-closed",
    non_raising=True,
)
def vague_term_wiring_count(options: Mapping[str, Any], *, user_term: str) -> int:
    """Count the resolvable ``vague_term`` wirings for ``user_term`` in a node's options.

    This is the single source of truth for "is a vague-term review actually
    resolvable?", shared by every staging-time gate so they cannot drift from
    the resolver contract. It mirrors the substitution wiring the resolver
    consumes in ``sessions/service.py::_patch_llm_transform_prompt``:

    * **Structured form** (``interpretation_requirements`` present): there must
      be exactly one *pending* ``vague_term`` requirement whose ``user_term``
      matches, AND a well-formed ``prompt_template_parts`` carrying at least one
      ``interpretation_ref`` part that references that requirement's ``id``.
      Returns ``1`` when wired; ``0`` when the requirement exists but no part
      references it (``prompt_template_parts`` absent, malformed, or missing the
      ref) — the deterministic root cause of the resolve-time 422 and the
      latent silent-drop; or the count of matching requirements when that count
      is not exactly one (caller treats any value ``!= 1`` as unresolvable).
    * **Legacy form** (no ``interpretation_requirements``): the number of
      ``{{interpretation:<user_term>}}`` placeholders in
      ``options.prompt_template``.

    Reads are lenient (Tier-3 staging idiom): a malformed sub-shape contributes
    ``0`` so the node reads as *unresolvable* and is routed back to the composer
    for repair, rather than crashing the request. Strict offensive validation
    lives at the resolve boundary, where the operator-approved mutation runs.
    """
    normalized_user_term = user_term.strip()
    requirements = options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None
    matching_ids: list[str] = []
    if isinstance(requirements, (list, tuple)):
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                continue
            if "status" not in requirement or requirement["status"] != "pending":
                continue
            if "user_term" not in requirement or not isinstance(requirement["user_term"], str):
                continue
            if requirement["user_term"].strip() != normalized_user_term:
                continue
            if (
                requirement["kind"] if "kind" in requirement else InterpretationKind.VAGUE_TERM.value
            ) != InterpretationKind.VAGUE_TERM.value:
                continue
            if "id" not in requirement:
                continue
            requirement_id = requirement["id"]
            if not isinstance(requirement_id, str) or not requirement_id:
                continue
            matching_ids.append(requirement_id)
    if len(matching_ids) == 1:
        requirement_id = matching_ids[0]
        parts = options[PROMPT_TEMPLATE_PARTS_KEY] if PROMPT_TEMPLATE_PARTS_KEY in options else None
        if not isinstance(parts, (list, tuple)):
            return 0
        ref_count = sum(
            1
            for part in parts
            if isinstance(part, Mapping)
            and ("kind" in part and part["kind"] == "interpretation_ref")
            and ("requirement_id" in part and part["requirement_id"] == requirement_id)
        )
        return 1 if ref_count >= 1 else 0
    if len(matching_ids) > 1:
        return len(matching_ids)
    # No matching pending vague_term requirement: the term is either wired by a
    # legacy ``{{interpretation:<term>}}`` placeholder (which coexists with the
    # auto-staged prompt-template / model-choice requirements) or not wired at
    # all. Mirror the resolver's legacy fall-back and count placeholders.
    prompt_template = options["prompt_template"] if "prompt_template" in options else None
    if isinstance(prompt_template, str):
        return sum(1 for term in _legacy_terms(prompt_template) if term == normalized_user_term)
    return 0


def _pending_authoring_shell(requirement: InterpretationRequirement) -> InterpretationRequirement:
    """Return the canonical persisted pending row without resolver evidence."""
    return {
        "id": requirement["id"],
        "kind": requirement["kind"],
        "user_term": requirement["user_term"],
        "status": "pending",
        "draft": requirement["draft"],
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }


def serialize_authoring_review_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return an audit-safe composer payload with only pending review shells."""
    serialized = dict(options)
    if "approved_prompt_artifact_hash" in serialized:
        del serialized["approved_prompt_artifact_hash"]
    if SOURCE_AUTHORING_KEY in serialized:
        del serialized[SOURCE_AUTHORING_KEY]
    review_index = _validated_review_index(options)
    requirements = tuple(review_index.values()) if review_index else None
    if requirements is not None:
        parts = _prompt_parts(options)
        for requirement in requirements:
            if requirement["status"] != "resolved" or InterpretationKind(requirement["kind"]) is not InterpretationKind.VAGUE_TERM:
                continue
            if parts is None or not any(
                part["kind"] == "interpretation_ref" and part["requirement_id"] == requirement["id"] for part in parts
            ):
                raise ValueError("resolved vague-term review cannot round-trip without reconstructible prompt_template_parts")
        compact_shells = [
            {
                "id": requirement["id"],
                "kind": requirement["kind"],
                "user_term": requirement["user_term"],
                "status": "pending",
                "draft": requirement["draft"],
            }
            for requirement in requirements
        ]
        serialized[INTERPRETATION_REQUIREMENTS_KEY] = compact_shells
        if parts is not None:
            coerced_shells: dict[str, InterpretationRequirement] = {
                str(shell["id"]): _coerce_requirement(shell) for shell in compact_shells
            }
            serialized["prompt_template"] = _render_prompt_parts(
                parts,
                coerced_shells,
                unresolved_text=PENDING_INTERPRETATION_AUTHORING_TEXT,
            )
    return serialized


def _review_identity(requirement: InterpretationRequirement) -> tuple[str, InterpretationKind, str]:
    return (
        requirement["id"],
        InterpretationKind(requirement["kind"]),
        requirement["user_term"].strip(),
    )


def _validated_review_index(options: Mapping[str, Any]) -> dict[tuple[str, InterpretationKind, str], InterpretationRequirement]:
    requirements = _requirements(options) or ()
    by_identity: dict[tuple[str, InterpretationKind, str], InterpretationRequirement] = {}
    by_id: set[str] = set()
    by_kind_term: set[tuple[InterpretationKind, str]] = set()
    for requirement in requirements:
        identity = _review_identity(requirement)
        kind_term = (identity[1], identity[2])
        if identity in by_identity or identity[0] in by_id or kind_term in by_kind_term:
            raise ValueError(f"duplicate interpretation requirement identity {identity!r}")
        by_identity[identity] = requirement
        by_id.add(identity[0])
        by_kind_term.add(kind_term)
    return by_identity


def _require_resolved_review_coherence(requirement: InterpretationRequirement) -> None:
    if requirement["status"] != "resolved":
        return
    event_id = requirement["event_id"]
    if type(event_id) is not str or not event_id.strip():
        raise ValueError(f"resolved interpretation requirement {requirement['id']!r} has no event_id")
    if type(requirement["accepted_value"]) is not str:
        raise ValueError(f"resolved interpretation requirement {requirement['id']!r} has no accepted_value")
    kind = InterpretationKind(requirement["kind"])
    if not resolved_review_evidence_is_coherent(requirement, kind):
        raise ValueError(f"resolved interpretation requirement {requirement['id']!r} has incoherent evidence")


def _node_review_artifact(
    node: NodeSpec,
    all_nodes: Sequence[NodeSpec],
    *,
    kind: InterpretationKind,
    user_term: str,
) -> str:
    if kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
        anchor_hash = prompt_review_anchor_hash_from_options(node.options)
        if anchor_hash is not None:
            return anchor_hash
        prompt_template = node.options["prompt_template"] if "prompt_template" in node.options else None
        if type(prompt_template) is not str:
            raise ValueError(f"llm_prompt_template review on node {node.id!r} has no prompt_template")
        return stable_hash(prompt_template)
    if kind is InterpretationKind.LLM_MODEL_CHOICE:
        model = node.options["model"] if "model" in node.options else None
        if type(model) is not str:
            raise ValueError(f"llm_model_choice review on node {node.id!r} has no model")
        return model_choice_artifact_hash(model)
    if kind is InterpretationKind.PIPELINE_DECISION:
        validate_pipeline_decision_node_semantics(
            node=node,
            all_nodes=all_nodes,
            user_term=user_term,
            draft=None,
            context="reconcile_authoritative_reviews",
        )
        return pipeline_decision_artifact_hash(node, all_nodes, user_term=user_term)
    raise ValueError(f"review kind {kind.value!r} does not have a node artifact hash")


def _resolved_review_hash(requirement: InterpretationRequirement, kind: InterpretationKind) -> str:
    field = resolved_review_evidence_field(kind)
    value = requirement[field]
    if type(value) is not str or not value:
        raise ValueError(f"resolved interpretation requirement {requirement['id']!r} has no {field}")
    return value


def _vague_review_is_unchanged(
    previous: NodeSpec,
    proposed: NodeSpec,
    requirement: InterpretationRequirement,
) -> bool:
    previous_parts = _prompt_parts(previous.options)
    proposed_parts = _prompt_parts(proposed.options)
    if previous_parts is None or proposed_parts is None:
        raise ValueError("resolved vague-term review cannot round-trip without prompt_template_parts")
    requirement_id = requirement["id"]
    if not any(part["kind"] == "interpretation_ref" and part["requirement_id"] == requirement_id for part in previous_parts):
        raise ValueError(f"resolved vague-term review {requirement_id!r} has no prompt_template_parts reference")
    if not any(part["kind"] == "interpretation_ref" and part["requirement_id"] == requirement_id for part in proposed_parts):
        raise ValueError(f"proposed vague-term review {requirement_id!r} has no prompt_template_parts reference")
    previous_prompt = previous.options["prompt_template"] if "prompt_template" in previous.options else None
    if type(previous_prompt) is not str:
        raise ValueError(f"resolved vague-term review {requirement_id!r} has no rendered prompt_template")
    # Both checks must stay invariant under SIBLING vague-term resolution: any
    # comparison against a hash frozen at this requirement's own resolution
    # moment goes permanently stale the instant a second term on the same
    # prompt resolves. So (1) the stored prompt must re-render from its own
    # parts + requirements (order-invariant tamper guard), and (2) the
    # requirement hash attests the accepted value alone.
    rendered_previous = _render_prompt_parts(
        previous_parts,
        _requirements_by_id(previous.options),
        unresolved_text=PENDING_INTERPRETATION_AUTHORING_TEXT,
    )
    if rendered_previous != previous_prompt:
        raise InterpretationReviewIntegrityError(
            f"resolved vague-term review {requirement_id!r} prompt drifted from its parts render",
            component_id=previous.id,
            component_type="transform",
            kind=InterpretationKind.VAGUE_TERM,
        )
    if _resolved_review_hash(requirement, InterpretationKind.VAGUE_TERM) != stable_hash(requirement["accepted_value"]):
        raise InterpretationReviewIntegrityError(
            f"resolved vague-term review {requirement_id!r} hash drifted",
            component_id=previous.id,
            component_type="transform",
            kind=InterpretationKind.VAGUE_TERM,
        )
    return prompt_structure_hash(previous_parts) == prompt_structure_hash(proposed_parts)


def _reconcile_node_options(
    previous: NodeSpec | None,
    proposed: NodeSpec,
    *,
    previous_nodes: Sequence[NodeSpec],
    proposed_nodes: Sequence[NodeSpec],
    proposed_graph: _OutputStreamGraph,
) -> Mapping[str, Any]:
    proposed_index = _validated_review_index(proposed.options)
    previous_index = _validated_review_index(previous.options) if previous is not None and previous.plugin == proposed.plugin else {}
    options = dict(proposed.options)
    if "approved_prompt_artifact_hash" in options:
        del options["approved_prompt_artifact_hash"]
    reconciled: list[Mapping[str, Any]] = []
    carried_prompt_review = False

    for identity, proposed_requirement in proposed_index.items():
        requirement_id, kind, user_term = identity
        shell = _pending_authoring_shell(proposed_requirement)
        if (
            kind is InterpretationKind.PIPELINE_DECISION
            and user_term == PROMPT_SHIELD_USER_TERM
            and node_has_capability(proposed, PluginCapability.LLM)
            and _llm_has_authorized_shield_upstream(proposed, proposed_graph)
        ):
            continue
        previous_requirement = previous_index[identity] if identity in previous_index else None
        if previous is None or previous_requirement is None or previous_requirement["status"] != "resolved":
            reconciled.append(shell)
            continue

        _require_resolved_review_coherence(previous_requirement)
        if kind is InterpretationKind.VAGUE_TERM:
            unchanged = _vague_review_is_unchanged(previous, proposed, previous_requirement)
        elif kind is InterpretationKind.INVENTED_SOURCE:
            raise ValueError("invented_source review cannot target a transform node")
        else:
            # The PREVIOUS node carries the resolved row, so a failure to derive
            # its artifact is drift of that review (the materializer's
            # pipeline-decision guard types the same failure). The PROPOSED
            # node's derivation below stays a plain edit refusal.
            try:
                previous_artifact = _node_review_artifact(previous, previous_nodes, kind=kind, user_term=user_term)
            except ValueError as exc:
                raise InterpretationReviewIntegrityError(
                    str(exc),
                    component_id=previous.id,
                    component_type="transform",
                    kind=kind,
                ) from exc
            stored_artifact = _resolved_review_hash(previous_requirement, kind)
            if stored_artifact != previous_artifact:
                raise InterpretationReviewIntegrityError(
                    f"resolved interpretation requirement {requirement_id!r} hash drifted",
                    component_id=previous.id,
                    component_type="transform",
                    kind=kind,
                )
            proposed_artifact = _node_review_artifact(proposed, proposed_nodes, kind=kind, user_term=user_term)
            unchanged = proposed_artifact == previous_artifact
        if unchanged:
            reconciled.append(dict(previous_requirement))
            carried_prompt_review = carried_prompt_review or kind in (
                InterpretationKind.VAGUE_TERM,
                InterpretationKind.LLM_PROMPT_TEMPLATE,
            )
        else:
            reconciled.append(shell)

    if reconciled:
        options[INTERPRETATION_REQUIREMENTS_KEY] = reconciled
    elif INTERPRETATION_REQUIREMENTS_KEY in options:
        del options[INTERPRETATION_REQUIREMENTS_KEY]

    parts = _prompt_parts(options)
    if parts is not None:
        requirements_by_id = _requirements_by_id(options)
        rendered = _render_prompt_parts(
            parts,
            requirements_by_id,
            unresolved_text=PENDING_INTERPRETATION_AUTHORING_TEXT,
        )
        options["prompt_template"] = rendered
    if carried_prompt_review:
        options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return options


def _reconcile_source_options(
    previous: SourceSpec | None,
    proposed: SourceSpec,
    *,
    component_id: str,
) -> Mapping[str, Any]:
    proposed_index = _validated_review_index(proposed.options)
    previous_index = _validated_review_index(previous.options) if previous is not None and previous.plugin == proposed.plugin else {}
    options = dict(proposed.options)
    reconciled: list[Mapping[str, Any]] = []
    for identity, proposed_requirement in proposed_index.items():
        requirement_id, kind, _user_term = identity
        shell = _pending_authoring_shell(proposed_requirement)
        previous_requirement = previous_index[identity] if identity in previous_index else None
        if kind is InterpretationKind.SOURCE_DATA_CONTRACT:
            # The acknowledged artifact binds the contract semantics and
            # demand FIELD SET, which are facts about the whole graph rather
            # than about this source's own options, so this per-source
            # reconciliation cannot judge graph drift. It can and must
            # validate the accepted-value/hash pair and require the proposed
            # source to retain the resolver-stamped guarantee before
            # preserving current authority. The pending-site enumerator
            # recomputes live graph demand on every read.
            if previous is None or previous_requirement is None or previous_requirement["status"] != "resolved":
                reconciled.append(shell)
                continue
            _require_resolved_review_coherence(previous_requirement)
            acknowledged_fields = resolved_source_data_contract_fields(previous_requirement)
            if acknowledged_fields is None:
                raise InterpretationReviewIntegrityError(
                    f"resolved interpretation requirement {requirement_id!r} evidence drifted",
                    component_id=component_id,
                    component_type="source",
                    kind=InterpretationKind.SOURCE_DATA_CONTRACT,
                )
            proposed_guaranteed_fields = _observed_source_guaranteed_fields(proposed.options)
            if proposed_guaranteed_fields is None or not frozenset(acknowledged_fields) <= proposed_guaranteed_fields:
                reconciled.append(shell)
                continue
            reconciled.append(dict(previous_requirement))
            continue
        if kind is not InterpretationKind.INVENTED_SOURCE:
            if previous_requirement is not None and previous_requirement["status"] == "resolved":
                raise ValueError(f"review kind {kind.value!r} cannot target source {component_id!r}")
            reconciled.append(shell)
            continue
        if previous is None or previous_requirement is None or previous_requirement["status"] != "resolved":
            reconciled.append(shell)
            continue

        _require_resolved_review_coherence(previous_requirement)
        previous_authoring = _source_authoring_metadata(previous.options)
        proposed_authoring = _source_authoring_metadata(proposed.options)
        if previous_authoring is None or proposed_authoring is None:
            raise ValueError("invented_source review requires reconstructible source_authoring metadata")
        stored_artifact = _resolved_review_hash(previous_requirement, kind)
        if stored_artifact != previous_authoring["content_hash"]:
            raise InterpretationReviewIntegrityError(
                f"resolved interpretation requirement {requirement_id!r} hash drifted",
                component_id=component_id,
                component_type="source",
                kind=InterpretationKind.INVENTED_SOURCE,
            )
        if proposed_authoring["content_hash"] == previous_authoring["content_hash"]:
            reconciled.append(dict(previous_requirement))
            options[SOURCE_AUTHORING_KEY] = dict(previous_authoring)
        else:
            reconciled.append(shell)

    if reconciled:
        options[INTERPRETATION_REQUIREMENTS_KEY] = reconciled
    elif INTERPRETATION_REQUIREMENTS_KEY in options:
        del options[INTERPRETATION_REQUIREMENTS_KEY]
    return options


def reconcile_authoritative_reviews(
    previous: CompositionState,
    proposed: CompositionState,
) -> CompositionState:
    """Rehydrate only coherent, still-applicable server-owned review evidence."""
    previous_sources = previous.sources
    reconciled_sources = {
        source_name: replace(
            source,
            options=_reconcile_source_options(
                previous_sources[source_name] if source_name in previous_sources else None,
                source,
                component_id=source_component_id(source_name),
            ),
        )
        for source_name, source in proposed.sources.items()
    }
    previous_nodes = {node.id: node for node in previous.nodes}
    proposed_graph = _output_stream_graph(proposed.nodes)
    reconciled_nodes = tuple(
        replace(
            node,
            options=_reconcile_node_options(
                previous_nodes[node.id] if node.id in previous_nodes else None,
                node,
                previous_nodes=previous.nodes,
                proposed_nodes=proposed.nodes,
                proposed_graph=proposed_graph,
            ),
        )
        for node in proposed.nodes
    )
    return replace(
        proposed,
        sources=reconciled_sources,
        nodes=reconciled_nodes,
    )


def model_choice_artifact_hash(model: str) -> str:
    """Canonical artifact hash for an operator-reviewed model identifier."""
    if type(model) is not str or not model.strip():
        raise ValueError("model_choice_artifact_hash requires a non-empty model identifier")
    return stable_hash(model)
