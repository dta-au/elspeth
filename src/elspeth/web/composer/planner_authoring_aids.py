"""Live-rendered planner authoring aids: worked exemplars from the live catalog.

The static skill pack deliberately carries no deployment plugin inventory (the
``no_deployment_plugin_facts`` gate enforces this), so worked ``set_pipeline``
exemplars — which must name real plugins — are rendered here at prompt-build
from the policy-visible catalog and ride in the planner's reviewed-context
user message. The exact objects rendered into the prompt are validated
through ``build_set_pipeline_candidate`` in
``tests/unit/web/composer/test_planner_authoring_aids.py``; an exemplar the
current validator rejects fails CI rather than teaching planners a dead shape.

Evidence base: the 2026-07-22 pack stress test (0/6 cold planners converged;
5/6 fabricated a ``blob_id``, 1/6 missed the source options contract). See
``scratch/planner-skill-pack-assessment.md``.

Exemplars are structural teaching, not solutions: they demonstrate wiring
(gate/fork/coalesce), custody binding, and review-row shapes in a neutral
domain deliberately disjoint from every live acceptance test. If a live
test's domain vocabulary ever appears in an exemplar, that test stops
measuring planner capability and starts measuring pack-lookup — the exemplar
has become the test's answer key and the acceptance signal is contaminated.
The rules text states principles generically and never names the exemplar's
domain fields as if they were required.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Any, Final, NotRequired, Required, TypedDict

from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginKind, PluginSchemaInfo, PluginSummary

# The registered shield-review constants and the untrusted-producer set are the
# contract's single source of truth (interpretation_state); importing them —
# private set included — is deliberate, so the taught row can never drift.
from elspeth.web.interpretation_state import (
    _UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS,
    PROMPT_SHIELD_AVAILABLE_DRAFT,
    PROMPT_SHIELD_USER_TERM,
    PROMPT_SHIELD_WARNING_DRAFT,
    RAW_HTML_CLEANUP_REVIEW_DRAFT,
    RAW_HTML_CLEANUP_USER_TERM,
    REGISTERED_PIPELINE_DECISION_USER_TERMS,
)
from elspeth.web.provider_config_policy import WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS

# The prompt never models a fabricated identifier — provenance is the lesson.
PLACEHOLDER_BLOB_ID: Final[str] = "<blob_id copied verbatim from a list_blobs or create_blob result>"

# Prompt-injection exposure follows every untrusted remote-text producer, but
# the raw-HTML/fingerprint cleanup contract is specific to web_scrape output.
# Keeping these producer sets separate prevents document extraction plugins
# such as Textract from inheriting a factually false audit-card draft.
_RAW_HTML_CLEANUP_PRODUCER_PLUGINS: Final[frozenset[str]] = frozenset({"web_scrape"})


class _PluginDigestEntry(TypedDict):
    """Closed policy-visible plugin summary rendered into the planner prompt.

    ``not_for`` carries the plugin's ``usage_when_not_to_use``; ``purpose`` and
    ``required_options`` likewise rename ``description`` and the required
    ``config_fields``. The digest names each field for what the planner does
    with it, so a parity sweep over the catalog vocabulary should grep this
    module rather than the field names alone.
    """

    name: str
    purpose: str
    required_options: list[str]
    not_for: NotRequired[str]
    capability_tags: NotRequired[list[str]]
    profile_aliases: NotRequired[list[str]]


class _DiscoveryDigest(TypedDict):
    """Closed plugin-kind inventory rendered from one catalog snapshot."""

    sources: list[_PluginDigestEntry]
    transforms: list[_PluginDigestEntry]
    sinks: list[_PluginDigestEntry]


class _SchemaContractEvidenceEntry(TypedDict):
    """One whole, current, policy-visible plugin contract."""

    plugin_id: str
    policy_hash: str
    snapshot_hash: str
    schema_hash: str
    json_schema: dict[str, object]
    knob_schema: dict[str, object]


class _SchemaContractEvidenceOmission(TypedDict):
    """Closed reason that a tracked or referenced contract is not present."""

    plugin_id: str
    reason: str


class _SchemaContractEvidence(TypedDict):
    """Bounded current-request schema evidence rendered for the planner."""

    policy_hash: str
    snapshot_hash: str
    max_entries: int
    max_omissions: int
    max_canonical_bytes: int
    canonical_bytes_used: int
    schemas: list[_SchemaContractEvidenceEntry]
    omitted: list[_SchemaContractEvidenceOmission]
    omissions_withheld_count: int


class _ExemplarSource(TypedDict, total=False):
    """Source subset used by the worked set-pipeline exemplars."""

    plugin: Required[str]
    on_success: Required[str]
    options: Required[dict[str, Any]]
    on_validation_failure: Required[str]
    inline_blob: dict[str, str]
    blob_id: str


class _ExemplarNode(TypedDict, total=False):
    """Union of node fields used by the worked set-pipeline exemplars."""

    id: Required[str]
    node_type: Required[str]
    plugin: str
    input: Required[str]
    on_success: str
    on_error: str
    options: dict[str, Any]
    condition: str
    routes: dict[str, str]
    fork_to: list[str]
    branches: dict[str, str]
    policy: str
    merge: str
    timeout_seconds: float


class _ExemplarEdge(TypedDict):
    """Optional UI edge shape accepted by set_pipeline exemplars."""

    id: str
    from_node: str
    to_node: str
    edge_type: str
    label: str | None


class _ExemplarOutput(TypedDict):
    """Sink subset used by the worked set-pipeline exemplars."""

    sink_name: str
    plugin: str
    options: dict[str, Any]
    on_write_failure: str


class _ExemplarMetadata(TypedDict):
    """Metadata carried by each worked set-pipeline exemplar."""

    name: str
    description: str


class _SetPipelineExemplar(TypedDict):
    """Closed top-level shape for worked set_pipeline arguments."""

    source: _ExemplarSource
    nodes: list[_ExemplarNode]
    edges: list[_ExemplarEdge]
    outputs: list[_ExemplarOutput]
    metadata: _ExemplarMetadata


class _SourceCustodyAid(TypedDict):
    rules: list[str]
    set_pipeline_exemplar_inline_blob: _SetPipelineExemplar
    existing_blob_source_binding: _ExemplarSource


class _ForkCoalesceAid(TypedDict):
    rules: list[str]
    set_pipeline_exemplar: _SetPipelineExemplar


class _ForkRowUnionAid(TypedDict):
    rules: list[str]
    set_pipeline_exemplar: _SetPipelineExemplar


class _RulesAid(TypedDict):
    rules: list[str]


class _ReviewRegistryAid(TypedDict):
    registered_pipeline_decision_user_terms: list[str]
    rules: list[str]


class _DiscoveryDigestAid(TypedDict):
    guidance: str
    plugins: _DiscoveryDigest


class _PlannerAuthoringAids(TypedDict, total=False):
    """Closed section vocabulary for the live planner-authoring payload."""

    purpose: Required[str]
    source_custody: _SourceCustodyAid
    fork_coalesce: _ForkCoalesceAid
    fork_row_union: _ForkRowUnionAid
    model_custody: _RulesAid
    llm_output_contract: _RulesAid
    llm_source_generation: _RulesAid
    review_registry: _ReviewRegistryAid
    prompt_shield: _RulesAid
    content_safety: _RulesAid
    raw_html_cleanup: _RulesAid
    web_scrape_http_identity: _RulesAid
    discovery_digest: _DiscoveryDigestAid


_SOURCE_CUSTODY_RULES: Final[tuple[str, ...]] = (
    "A blob_id comes ONLY from blob-tool output in this session (list_blobs, "
    "list_composer_blobs, create_blob, get_blob_metadata). Copy it verbatim.",
    "If no tool returned the identifier, bind the data with source.inline_blob "
    "(filename, mime_type, content) or create_blob first. Never fabricate a "
    "blob_id, secret reference, model identifier, or any other identifier.",
    "inline_blob.content must be the user's data verbatim, exactly as it "
    "appears in their message; custody records it against that message.",
    "Custody owns the storage binding: author schema.mode and on_validation_failure on a blob-bound source, never path or blob_ref.",
    "A file the user NAMES but never uploaded, whose content is not in the "
    "conversation, has NO legal binding: source paths must resolve to real "
    "session-blob storage, so an invented path is always rejected. Discover "
    "first (list_blobs / list_composer_blobs); if nothing matches, ask one "
    "narrow upload/selection question — or, on a surface with no question "
    "channel, decline with a named gap. Never fabricate a path, blob id, or "
    "stand-in rows.",
    # run-2 G4: the digest hands a discovered blobs/<name> PATH but nothing
    # said which slot it binds to; sims put it in blob_id (UUID-typed) and died.
    "A DISCOVERED operator file path (a blobs/<name> the session's discovery "
    "facts or blob metadata handed you) binds through source options.path, "
    "copied verbatim. source.blob_id accepts ONLY the UUID a blob tool "
    "returned this session — a path in blob_id is always rejected.",
)

_INLINE_EXEMPLAR_FILENAME: Final[str] = "stock_levels.csv"
_INLINE_EXEMPLAR_MIME: Final[str] = "text/csv"
_INLINE_EXEMPLAR_CONTENT: Final[str] = "sku,on_hand\nAX-100,12\nBX-204,7\n"

_FORK_COALESCE_RULES: Final[tuple[str, ...]] = (
    # run-3 E1: the old unconditional fork-over-queries preference produced a
    # wrong-topology V-pass and 0/3 multi_query adoption where it fit. Shape
    # SELECTION is decided by what varies per assessment, not by preference.
    "SHAPE SELECTION: several assessments of the SAME input field (aspects, "
    "angles, questions about one piece of content) belong on a SINGLE llm "
    "node's queries map (multi_query) — one node, one pass, prefixed output "
    "fields. Use THIS fork/coalesce shape only when the branches take "
    "genuinely INDEPENDENT inputs or independent per-branch processing "
    "chains (different fields, different upstream transforms, different "
    "plugins per branch).",
    "When the user explicitly asks for separate nodes or one model call per "
    "branch over independent inputs, fork with a gate fanning out to one "
    "branch transform per branch, rejoined by a coalesce.",
    "Key the coalesce branches map by FORK BRANCH NAME; each value names the "
    "connection arriving at the coalesce after that branch's transforms.",
    "When branches rejoin at a coalesce, each branch transform MUST publish "
    "the connection named in the coalesce's branches values. A branch "
    "transform must never publish to a sink — only the coalesce's downstream "
    "path reaches sinks.",
    "A coalesce publishes its merged rows under its own node id — the "
    "downstream consumer sets input to the coalesce id. Do not author "
    "on_success on a coalesce unless it routes directly to a sink.",
    "Give each branch transform its own output field (an llm node's response_field) so the union merge carries every branch's result on one row.",
    "Do not author interpretation_requirements rows for llm_prompt_template "
    "or llm_model_choice — required LLM reviews auto-stage on every llm "
    "node. Author rows only for the planner-owned kinds (vague_term wired "
    "via prompt_template_parts, registered pipeline_decision, "
    "invented_source), each in the short form {kind, user_term, draft} with "
    "user_term mandatory.",
    "Coalesce policy and merge are the engine's closed vocabularies: policy "
    "is one of require_all, quorum, best_effort, first; merge is one of "
    "union, nested, select. Use best_effort when some branches may "
    "legitimately produce no row for an id — it merges whichever branches "
    "arrive, where require_all would drop the whole row.",
    "A coalesce consumes ONLY the connections named in its branches values. "
    "Its input field is required by the schema but is not a consuming "
    "binding — set it to the first branch's arriving connection by "
    "convention, never to a name nothing publishes.",
)

_FORK_EXEMPLAR_CONTENT: Final[str] = "ticket_id,body\nT-1001,Cannot log in since the update\nT-1002,Invoice totals look wrong\n"
_ROW_UNION_EXEMPLAR_CONTENT: Final[str] = "case_id,variant_text\nC-1001,Control copy\nC-1002,Treatment copy\n"

_FORK_ROW_UNION_RULES: Final[tuple[str, ...]] = (
    "Use row_union for require_all N-to-N reconvergence: every fork branch contributes its original rows, "
    "and the barrier releases all of them in declared branch order without merging fields.",
    "Key branches by the upstream gate's fork_to branch names. Each value is the unique connection published "
    "by that branch's final transform.",
    "Set input to the first branch connection exactly. It is an adapter placeholder; all branches values are the real consuming bindings.",
    "Set on_success to a downstream processing connection, never directly to a sink. Omit plugin, options, "
    "on_error, policy, merge, gate routing, and aggregation fields.",
    "timeout_seconds is optional; when present it must be finite and greater than zero.",
)


def _prompt_shield_rules(
    *,
    shield_plugin: str | None,
    shield_required: bool = False,
    shield_auto_wired: bool = False,
    untrusted_producers: tuple[str, ...],
) -> list[str]:
    """Shield-staging rules quoting the registered review constants verbatim.

    Required mode is auto-wired server-side (R2-F10, elspeth-f99655f540):
    ``required_controls.wire_required_controls`` splices the selected shield
    onto any uncovered llm input at proposal time and stages a
    ``required_control_auto_wired`` disclosure card, so the aids teach the
    guarantee instead of demanding manual wiring — and drop the
    shield-recommendation row, whose exposure the wired shield removes.
    Recommend mode stages the advisory review row whether or not an
    implementation is selected, using the deployment-available draft when it
    is. The review is ADVISORY end-to-end (warnings only, excluded from the
    blocking contract), so no rejection code ever teaches it on a repair
    turn — these aids are the only lever. Tutorial finalizer battery (dim_c
    under-flag): the replan planner non-deterministically omitted the row on
    the scrape→summarize llm node. Constants are imported from
    ``interpretation_state`` so the taught row can never drift from the
    contract (the 52322ebe1 discipline); the draft is chosen by the LIVE
    snapshot's shield selection, mirroring the warning→available upgrade the
    server itself applies, and memoizes correctly because the aids cache is
    keyed by snapshot hash.
    """
    producers = " or ".join(sorted(untrusted_producers))
    if shield_plugin is not None and shield_required and shield_auto_wired:
        return [
            f"This deployment REQUIRES a prompt-injection shield and has selected {shield_plugin}. "
            "You do not need to wire it yourself: when a proposal's llm input is not already "
            f"covered, ELSPETH automatically splices a {shield_plugin} transform onto that input "
            "edge and stages a required_control_auto_wired disclosure card for the operator to "
            f"acknowledge. You MAY wire a {shield_plugin} transform explicitly (between the "
            f"{producers} producer and the llm node) when you want to control its placement; the "
            "auto-wire pass leaves a covered graph untouched.",
            "Auto-wiring can only scope the shield to PROVABLE prompt fields: keep every prompt "
            "row access static ('{{ row.field }}', never '{{ row[key] }}') so the protected field "
            "set can be derived.",
            f"Do NOT stage the {PROMPT_SHIELD_USER_TERM} review row on those llm nodes — with the "
            "shield wired (by you or by the auto-wire pass) the exposure it warns about no longer "
            "exists.",
        ]
    if shield_plugin is not None and shield_required:
        # REQUIRED and selected, but not auto-wirable: the selection is
        # alias-less and its required service bindings only exist as
        # placeholder exemplars, which must never become real node config. The
        # manual-wiring mandate stays the teaching for this posture.
        return [
            f"An authorized prompt-injection shield is available in this deployment: {shield_plugin}. "
            f"When an llm transform consumes externally-controlled content (any path from a {producers} "
            f"output reaches its input), WIRE a {shield_plugin} transform between that producer node and "
            "the llm node — its input is the producer node's on_success connection, and its on_success "
            "is the llm node's input. This is required, not advisory: untrusted text must not "
            "reach the model unshielded.",
            "Load the shield's schema and assistance through the capability catalog before authoring "
            "it, and configure it from that schema alone.",
            f"With the shield wired, do NOT also stage the {PROMPT_SHIELD_USER_TERM} review row — the "
            "exposure it warns about no longer exists.",
        ]
    draft = PROMPT_SHIELD_AVAILABLE_DRAFT if shield_plugin is not None else PROMPT_SHIELD_WARNING_DRAFT
    availability = (
        f"The selected deployment implementation is {shield_plugin}. "
        if shield_plugin is not None
        else "No deployment implementation is selected. "
    )
    return [
        availability,
        f"When an llm transform consumes externally-controlled content (any path from a {producers} "
        "output reaches its input), stage the prompt-injection shield review ON THAT LLM NODE: "
        "add one pending pipeline_decision entry to its options.interpretation_requirements "
        "(a sibling of the node's other options).",
        f'Use exactly: {{"kind": "pipeline_decision", "user_term": "{PROMPT_SHIELD_USER_TERM}", "draft": "{draft}"}} '
        "— copy the user_term and draft strings verbatim.",
        "The review is advisory and never blocks the pipeline, but omitting it hides a "
        "prompt-injection exposure decision from the operator's review cards.",
        "Skip the row only when an authorized prompt-injection shield transform is already wired between that producer node and the llm node.",
    ]


def _content_safety_rules(*, safety_plugin: str, auto_wired: bool = True) -> list[str]:
    """Wiring rules for the content-safety control this deployment selected.

    Same regime as the shield's available branch, on the other side of the
    model: the shield protects what goes IN, content safety screens what
    comes OUT. Only the available branch exists today — there is no
    registered ``pipeline_decision`` term for an absent content-safety
    control, so a deployment without one gets no acknowledge card (unlike
    the shield). That asymmetry is a known gap, not a decision.

    Coverage is checked over EVERY output edge of the llm node, not just
    ``on_success``, so the on_success-only framing this used to carry was a
    half-truth that produced an unrepairable rejection: an author who wired the
    control exactly as told and quarantined failures to a sink was rejected for
    an edge these rules never mentioned.

    Like the shield's required branch, the on_success edge is auto-wired
    server-side (R2-F10) when the selection is actually deployable (an
    operator profile alias, or direct options fully bound to real
    deployment values): the aids then teach the guarantee and keep only the
    on_error discipline, which the auto-wire pass cannot repair — no control
    can sit on an error branch, so a quarantine sink stays an operator
    decision. A selection whose required bindings only exist as placeholder
    exemplars cannot be auto-wired, so that posture keeps the manual-wiring
    mandate.
    """
    on_error_rule = (
        f"Screening is checked on EVERY output edge of the llm node, not just on_success. An "
        f"on_error edge names a SINK (or 'discard') and nothing else, so it cannot pass through "
        f"{safety_plugin} — set the llm node's on_error to 'discard', and likewise for any "
        f"transform between the llm node and {safety_plugin}. Only downstream OF the "
        f"{safety_plugin} transform may on_error name a quarantine sink. Keeping failed llm rows "
        "in a quarantine sink is an operator decision (relax the control mode, or run under the "
        "CLI/batch runtime), never something to author around.",
    )
    if auto_wired:
        return [
            f"This deployment REQUIRES the {safety_plugin} content-safety control on every path "
            "carrying llm output. You do not need to wire it yourself: when a proposal's llm "
            f"on_success path is not already covered, ELSPETH automatically splices a {safety_plugin} "
            "transform onto that edge and stages a required_control_auto_wired disclosure card for "
            f"the operator to acknowledge. You MAY wire a {safety_plugin} transform explicitly when "
            "you want to control its placement; the auto-wire pass leaves a covered graph untouched.",
            *on_error_rule,
        ]
    return [
        f"An authorized content-safety control is available in this deployment: {safety_plugin}. "
        f"WIRE a {safety_plugin} transform on the llm node's on_success output — its input is the "
        "llm node's on_success connection, and its on_success carries the screened rows onward. "
        "This is required, not advisory: model-generated content must be screened before it is "
        "written out.",
        *on_error_rule,
        "Load its schema and assistance through the capability catalog before authoring it, and configure it from that schema alone.",
    ]


def _raw_html_cleanup_rules(*, untrusted_producers: tuple[str, ...]) -> list[str]:
    """Raw-HTML cleanup review rules quoting the registered constants verbatim.

    run-2 G6: the shield draft shipped verbatim (reproduced 3/3) while the
    cleanup draft did not — sims paraphrased and the contract's marker
    recognition ("raw html" + "fingerprint" substrings) treated the row as
    absent, re-firing interpretation_review_contract_unsatisfied. Same
    imported-constants discipline as the shield rules.
    """
    producers = " or ".join(sorted(untrusted_producers))
    return [
        f"When a field_mapper with select_only=true drops {producers} raw fields "
        "(raw content / fingerprint) before the sink, stage the cleanup review ON "
        "THAT field_mapper node: one pending pipeline_decision entry in its "
        "options.interpretation_requirements (a SIBLING of mapping, never inside it).",
        f'Use exactly: {{"kind": "pipeline_decision", "user_term": "{RAW_HTML_CLEANUP_USER_TERM}", "draft": "{RAW_HTML_CLEANUP_REVIEW_DRAFT}"}} '
        "— copy the user_term and draft strings verbatim.",
        "The row is RECOGNIZED only when the draft text names both the raw HTML "
        "and the fingerprint fields — a paraphrased draft is treated as absent "
        "and the same rejection fires again.",
        "An explicit user instruction to drop the fields does NOT waive the row — it records that decision for the audit trail.",
    ]


_WEB_SCRAPE_HTTP_IDENTITY_RULES: Final[tuple[str, ...]] = (
    # run-3 E3 (mechanical half; hard-fail-vs-review doctrine is an operator
    # item — enforcement is NOT changed here).
    "http.abuse_contact and http.scraping_reason are IDENTITY CLAIMS made to "
    "remote site operators. Bind them ONLY from identities discoverable in "
    "this session — operator-provided discovery facts, session context, or "
    "the user's own words — copied verbatim.",
    "If no discoverable identity exists, OMIT the http block entirely and "
    "name the gap in metadata.description; the coded rejection for a missing "
    "required block is the correct outcome. NEVER invent a plausible "
    "identity: validation enforcement is reserved-list-only (known-reserved "
    "domains hard-fail), so a fabricated contact can pass validation and "
    "SHIP a false claim — passing the validator does not make it yours to "
    "assert.",
)


def _model_custody_rules(profile_alias: str | None) -> list[str]:
    """Model-provisioning custody with the sanctioned alternative rendered live.

    Suite run 1 G2 (8/8 problems): the pack's never-invent-a-slug rule had no
    sanctioned alternative — obeying it (omitting the model binding) was
    validator-fatal while inventing a literal slug passed. The operator-profile
    path is that alternative: ``options.profile`` binds the model through
    operator policy and exempts the ``llm_model_choice`` review
    (``interpretation_state.materialize_state_for_execution`` derives the
    exemption from the profile binding). The live alias is rendered here so the
    rule is actionable offline, not a demand for aliases the deployment does
    not serve.
    """
    rules: list[str] = []
    if profile_alias is not None:
        rules.append(
            f"This deployment serves the llm operator profile alias '{profile_alias}'. "
            "Author llm nodes with options.profile set to that alias and OMIT "
            "model/provider/credential options entirely: operator policy supplies "
            "the concrete model, and a profile-bound node carries NO "
            "llm_model_choice review card."
        )
    else:
        rules.append(
            "No llm operator profile is currently usable in this deployment: "
            "bind a model only through a literal slug that list_models served "
            "in THIS session."
        )
    rules.append(
        "Author options.model ONLY with a slug served by a list_models call — "
        "never invented, never recalled from training. A literal slug "
        "auto-stages the llm_model_choice review, which must be surfaced and "
        "resolved before the pipeline can run."
    )
    rules.append(
        "Omitting the model binding entirely is not compliance — an llm node "
        "needs either options.profile or a discovery-served options.model."
    )
    return rules


# run-2 G9: web policy rejects sequential (pool_size 1) multi_query llm nodes
# with unbounded capacity retries; the ceiling is imported so the taught
# number can never drift from provider_config_policy.
# run-3 P2 correction: the previous unconditional form of this rule steered
# PROFILE-bound authors into an operator-private option (pool_size /
# max_capacity_retry_seconds) and a profile_unavailable rejection — the
# operator layer auto-injects the web-safe retry bound on profile-bound
# multi_query nodes (profiles.lower_options). The rule is form-conditional.
_WEB_MULTI_QUERY_RETRY_RULE: Final[str] = (
    "On a PROFILE-bound llm node (options.profile), never author pacing or "
    "retry options — pool_size and max_capacity_retry_seconds are "
    "operator-private and the profile layer injects the web-safe retry bound "
    "for queries automatically. Only a provider-form llm node must bound "
    f"sequential multi_query retries itself: max_capacity_retry_seconds <= {WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS} "
    "or pool_size > 1."
)

# The on_error advice is control-mode conditional. Taught unconditionally, it
# contradicted the required-output-control gate: it steered planners to route an
# llm node's failures to a quarantine sink, which required_control_coverage then
# (correctly) rejects as an uncontrolled write path. The two variants are
# module constants so the gating is testable without asserting prose.
_LLM_ON_ERROR_QUARANTINE_RULE: Final[str] = (
    "on_error='discard' silently drops failed rows. When the user needs "
    "failures retained or inspected, route on_error to a dedicated "
    "quarantine sink instead of discard."
)

# Rendered with the deployment's selected output control. A quarantine sink is
# genuinely unavailable to an llm node here: on_error may only name a sink or
# 'discard' (core/dag/builder.py:1108), so no control transform can be
# interposed on an error branch, and any sink it names is an uncontrolled write.
_LLM_ON_ERROR_CONTROLLED_RULE_TEMPLATE: Final[str] = (
    "This deployment REQUIRES the {control} control on every path carrying llm "
    "output, so an llm node's on_error MUST be 'discard'. An on_error edge "
    "names a SINK (or 'discard') and nothing else — no control transform can "
    "sit on an error branch — so a quarantine sink for an llm node is an "
    "uncontrolled write path and the pipeline is rejected. Do NOT try to wire "
    "{control} onto the error branch; that connection has no producer and fails "
    "graph construction. The same applies to any transform between the llm node "
    "and the {control} transform; downstream OF that control, on_error may name "
    "a quarantine sink normally, as may transforms that never carry llm output."
)

# The author cannot satisfy "retain the failed rows" here, so the rule names who
# can. Without this the planner reads the requirement as a bug and burns its
# repair budget re-attempting quarantine shapes.
_LLM_ON_ERROR_CONTROLLED_TRADEOFF_TEMPLATE: Final[str] = (
    "'discard' costs the failed row's CONTENT, not the record of it: nothing "
    "reaches a sink to inspect later, while the audit trail still records the "
    "row's terminal outcome and content hash. If the user needs failed llm rows "
    "preserved in a quarantine sink, say plainly that this is an operator "
    "decision, not something to author around — the operator relaxes the "
    "{control} control mode to 'recommend', or the pipeline runs under the "
    "CLI/batch runtime. Do not keep re-attempting quarantine shapes."
)


def _llm_on_error_rules(*, output_control: str | None) -> list[str]:
    """Pick the on_error rules the deployment's control posture makes authorable."""
    if output_control is None:
        return [_LLM_ON_ERROR_QUARANTINE_RULE]
    return [
        _LLM_ON_ERROR_CONTROLLED_RULE_TEMPLATE.format(control=output_control),
        _LLM_ON_ERROR_CONTROLLED_TRADEOFF_TEMPLATE.format(control=output_control),
    ]


_LLM_OUTPUT_CONTRACT_RULES: Final[tuple[str, ...]] = (
    "An llm node writes the model's reply as ONE raw string into the field "
    "named by options.response_field (default llm_response). Prompt text that "
    "asks for JSON or named keys does NOT create row fields — nothing is "
    "flattened out of the reply.",
    "Downstream nodes may require only that response field (plus fields "
    "passed through from the node's input). To obtain several named result "
    "fields from one llm node, use the plugin's multi_query mechanism — its "
    "schema declares the per-query output fields, and it is the ONLY blessed "
    "multi-field shape.",
    "If a prompt asks for structured JSON anyway, the JSON arrives as one "
    "string in the response field; wire a schema-proven parser transform "
    "when downstream nodes need its keys as row fields.",
    # ── multi_query QueryDefinition contract (run-2 G2: the blessed-shape
    # mandate above shipped without the shape's contract) ─────────────────
    "queries is a mapping of query name to a query OBJECT (list form needs "
    "name in each entry). Every query object REQUIRES input_fields — a "
    "mapping of template variable name to row column name, e.g. "
    '{"field_a": "field_a", "field_b": "field_b"} — a query without '
    "input_fields is rejected.",
    "The per-query prompt key is 'template' (a Jinja2 override), NOT "
    "prompt_template. The top-level options.prompt_template is STILL "
    "required and is the fallback for any query that omits template.",
    # run-3 E2: the output contract — each query KEY prefixes its output row
    # fields, so downstream nodes can require them by exact name.
    "Each query key names its output row fields by PREFIX: the raw reply "
    "lands in <query_key>_<response_field>, and each typed output_fields "
    "entry lands in <query_key>_<suffix>. Downstream mappers and sinks "
    "reference those exact prefixed names.",
    # run-4 E1: the CLOSED per-query key set + namespace arbitration.
    # run-5 P2 correction: the run-4 set omitted response_format, max_tokens,
    # and list-form name — all valid QueryDefinition keys — so the rule
    # forbade supported configuration by omission and steered planners to
    # drop it or produce a nameless list entry validation rejects.
    "The per-query keys you author are input_fields (REQUIRED), template, "
    "output_fields, response_format ('standard' or 'structured'; default "
    "standard), and max_tokens (a per-query override of the node-level "
    "max_tokens). In mapping form the mapping key supplies the query name; "
    "in LIST form each entry additionally REQUIRES its own name key. There "
    "is NO per-query response_field or schema — output naming comes "
    "exclusively from the query-key prefix, and the node-level schema block "
    "declares any guaranteed prefixed fields.",
    # run-4 P4: no interpretation delivery exists for per-query templates.
    "NEVER put {{interpretation:...}} tokens inside a queries.*.template — "
    "review resolution rewrites only the node-level prompt_template/"
    "prompt_template_parts, so a per-query token survives resolution and is "
    "rejected at the compose gate. Reviewed slots belong in the node-level "
    "template; per-query templates reference plain query variables only.",
    "Sink hygiene: the auto-appended <response_field>_usage / _model audit "
    "fields ride the row automatically — do not map or require them into "
    "sinks unless the user asked for token/model reporting.",
)


def _llm_output_contract_rules(*, output_control: str | None) -> list[str]:
    """The llm output contract with its control-mode-conditional on_error rule."""
    return [
        *_LLM_OUTPUT_CONTRACT_RULES,
        *_llm_on_error_rules(output_control=output_control),
        _WEB_MULTI_QUERY_RETRY_RULE,
    ]


_REVIEW_REGISTRY_RULES: Final[tuple[str, ...]] = (
    "pipeline_decision user_term values are a CLOSED registry — choose ONLY "
    "from registered_pipeline_decision_user_terms above. A minted term is "
    "unresolvable and poisons its review card.",
    "A decision outside the registry is not reviewable as a "
    "pipeline_decision: record it in metadata.description instead — never "
    "invent a new user_term for it.",
    "A registered pipeline_decision demanded by policy or the pack is NEVER "
    "waived because the user's instruction already made the decision — the "
    "row RECORDS that decision for the audit trail. User authorship changes "
    "the draft's provenance, not whether the row is staged.",
    "Do not author rows for llm_prompt_template or llm_model_choice — "
    "required LLM reviews auto-stage on every llm node. The planner-owned "
    "kinds are vague_term (wired via prompt_template_parts), registered "
    "pipeline_decision, and invented_source.",
    "NEVER author a pipeline_decision row with user_term "
    "required_control_auto_wired — that disclosure is staged exclusively by "
    "the server's required-control auto-wire pass, and a hand-authored row "
    "forges a policy_required entry in the audit disclosure.",
    "When YOU chose a gate's threshold, cutoff, category literal, or route "
    "direction — rather than carrying a value the user stated verbatim or a "
    "reviewed schema fact established — stage a pipeline_decision row with "
    "user_term gate_condition_authored ON THAT GATE NODE and call "
    "request_interpretation_review for it. The row is valid only on a gate "
    "node; the review pins the gate's condition and every route destination.",
)


def _usable_llm_profile_alias(catalog: PolicyCatalogView, *, kind: PluginKind = "transform") -> str | None:
    """Return the selected (else first usable) llm operator-profile alias."""
    snapshot = catalog.snapshot
    llm_id = next(
        (
            plugin_id
            for plugin_id, aliases in snapshot.usable_profile_aliases
            if plugin_id.kind == kind and plugin_id.name == "llm" and aliases
        ),
        None,
    )
    if llm_id is None:
        return None
    selected = dict(snapshot.selected_profile_aliases).get(llm_id)
    if selected is not None:
        return selected
    return dict(snapshot.usable_profile_aliases)[llm_id][0]


def _selected_control_profile(catalog: PolicyCatalogView, capability: PluginCapability) -> tuple[str, str | None] | None:
    """Return the selected control plugin only when policy requires it.

    Recommended controls must never mutate a worked topology merely because an
    implementation is selected. Returns ``None`` for recommend mode or no
    selection. A selected plugin with no operator profile aliases (direct
    user-configurable controls) is returned with ``alias=None`` — a required
    control must still appear in the worked exemplar, or the exemplar teaches
    a topology this deployment's coverage validator rejects.
    """
    snapshot = catalog.snapshot
    if dict(snapshot.control_modes).get(capability, ControlMode.RECOMMEND) is not ControlMode.REQUIRED:
        return None
    plugin_id = dict(snapshot.selected).get(capability)
    if plugin_id is None:
        return None
    alias = dict(snapshot.selected_profile_aliases).get(plugin_id)
    if alias is None:
        aliases = dict(snapshot.usable_profile_aliases).get(plugin_id, ())
        alias = aliases[0] if aliases else None
    return plugin_id.name, alias


# Deployment-owned service bindings a direct-config control still requires.
# Placeholder values that pass option prevalidation, in the same spirit as
# PLACEHOLDER_BLOB_ID — the planner substitutes the deployment's real binding
# supplied by the user.
_DIRECT_CONTROL_OPTION_EXEMPLARS: Final[dict[str, dict[str, object]]] = {
    "azure_prompt_shield": {"endpoint": "https://your-resource.cognitiveservices.azure.com"},
    "azure_content_safety": {
        "endpoint": "https://your-resource.cognitiveservices.azure.com",
        # The plugin's documented example thresholds — an effective blocking
        # posture (all-6 thresholds are a no-op the coverage validator rejects).
        "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 0},
    },
}


def _direct_control_options(summaries: Mapping[str, list[PluginSummary]], plugin_name: str) -> dict[str, object]:
    """Required direct-config options for an alias-less control node.

    A required control selected without operator profile aliases is authored
    directly, so the exemplar must carry the plugin's remaining required
    options or ``set_pipeline`` prevalidation rejects it. Declared credential
    fields are wired as ``{"secret_ref": NAME}`` markers using the plugin's
    canonical inventory candidate — the supported inline new-node form; the
    remaining required service bindings come from the placeholder table.
    """
    plugin = next(entry for entry in summaries["transform"] if entry.name == plugin_name)
    candidates_by_field = {requirement.field: requirement.candidates for requirement in plugin.secret_requirements}
    placeholders = _DIRECT_CONTROL_OPTION_EXEMPLARS.get(plugin_name, {})
    options: dict[str, object] = {}
    for field in plugin.config_fields:
        if not field.required:
            continue
        candidates = candidates_by_field.get(field.name)
        if candidates:
            options[field.name] = {"secret_ref": candidates[0]}
        elif field.name in placeholders:
            options[field.name] = placeholders[field.name]
    return options


def _direct_control_options_are_deployable(summaries: Mapping[str, list[PluginSummary]], plugin_name: str) -> bool:
    """Whether an alias-less control's required options are REAL deployment bindings.

    ``_direct_control_options`` fills required fields from two sources: the
    plugin's canonical secret-ref inventory (real deployment bindings — the
    ref must exist for the plugin to be available) and the
    ``_DIRECT_CONTROL_OPTION_EXEMPLARS`` placeholder table, which exists ONLY
    so worked exemplars prevalidate — the planner is expected to substitute
    the deployment's real value. A placeholder must never become persisted
    node config: Azure endpoint validation is suffix-only, so a wired
    ``https://your-resource...`` endpoint clears every gate while pointing a
    live secret_ref at a third-party-registrable resource. Returns False when
    any required field (beyond the ones the caller authors itself: fields /
    schema / source) would come from the placeholder table or be left
    unfilled — the auto-wire pass then treats the selection as
    REQUIRED-but-unselected and the aids keep teaching manual wiring.
    """
    plugin = next(entry for entry in summaries["transform"] if entry.name == plugin_name)
    candidates_by_field = {requirement.field: requirement.candidates for requirement in plugin.secret_requirements}
    caller_authored = {"fields", "schema", "source"}
    for field in plugin.config_fields:
        if not field.required or field.name in caller_authored:
            continue
        if candidates_by_field.get(field.name):
            continue
        return False
    return True


def _plugin_declares_field(summaries: Mapping[str, list[PluginSummary]], plugin_name: str, field_name: str) -> bool:
    """Return True when the named transform declares that config field."""
    return any(
        field.name == field_name for plugin in summaries["transform"] if plugin.name == plugin_name for field in plugin.config_fields
    )


def _plugin_summaries(catalog: PolicyCatalogView) -> dict[str, list[PluginSummary]]:
    """One catalog sweep shared by every aid — the expensive step of a build."""
    return {
        "source": catalog.list_sources(),
        "transform": catalog.list_transforms(),
        "sink": catalog.list_sinks(),
    }


def _visible_plugin_names(
    catalog: PolicyCatalogView, summaries: Mapping[str, list[PluginSummary]] | None = None
) -> dict[str, frozenset[str]]:
    if summaries is None:
        summaries = _plugin_summaries(catalog)
    return {kind: frozenset(plugin.name for plugin in plugins) for kind, plugins in summaries.items()}


# The evidence rides in one dynamic prompt message, so it must be bounded even
# when a deployment installs many rich plugins. Entries are indivisible: a
# contract that does not fit is reported as omitted, never sliced into a shape
# that could be mistaken for the plugin's whole option contract.
_SCHEMA_EVIDENCE_MAX_ENTRIES: Final[int] = 8
_SCHEMA_EVIDENCE_MAX_OMISSIONS: Final[int] = 16
_SCHEMA_EVIDENCE_MAX_CANONICAL_BYTES: Final[int] = 96 * 1024

_JSON_SCHEMA_PROSE_KEYS: Final[frozenset[str]] = frozenset(
    {"$comment", "title", "description", "examples", "example", "composer_description", "composer_placeholder"}
)
_JSON_SCHEMA_SCALAR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
        "$vocabulary",
        "type",
        "const",
        "enum",
        "default",
        "pattern",
        "format",
        "contentEncoding",
        "contentMediaType",
        "deprecated",
        "readOnly",
        "writeOnly",
        "nullable",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "minContains",
        "maxContains",
    }
)
_JSON_SCHEMA_MAP_KEYS: Final[frozenset[str]] = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
_JSON_SCHEMA_SINGLE_SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "items",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contentSchema",
    }
)
_JSON_SCHEMA_SCHEMA_LIST_KEYS: Final[frozenset[str]] = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


class _SchemaContractProjectionUnsupported(ValueError):
    """A schema carries semantics this bounded projection cannot preserve."""


# Public name for callers that must fail closed without depending on an
# implementation-private spelling. The private alias remains the trust-tier
# boundary's established exception identity.
SchemaContractProjectionUnsupported = _SchemaContractProjectionUnsupported

_PLANNER_CONTRACT_MAX_DEPTH: Final[int] = 32
_PLANNER_CONTRACT_MAX_NODES: Final[int] = 4096
_PLANNER_CONTRACT_MAX_CANONICAL_BYTES: Final[int] = 48 * 1024


def _assert_projection_input_bounds(value: object) -> None:
    """Reject recursive or oversized provider shapes before projection."""
    pending: list[tuple[object, int]] = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > _PLANNER_CONTRACT_MAX_DEPTH or visited > _PLANNER_CONTRACT_MAX_NODES:
            raise _SchemaContractProjectionUnsupported
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


@dataclass(frozen=True, slots=True)
class PlannerPluginContract:
    """Owned bounded planner projection of one admitted plugin schema."""

    plugin_id: str
    schema_hash: str
    json_schema: Mapping[str, object]
    knob_schema: Mapping[str, object]
    composer_hints: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "schema_hash": self.schema_hash,
            "json_schema": deepcopy(self.json_schema),
            "knob_schema": deepcopy(self.knob_schema),
            "composer_hints": list(self.composer_hints),
        }


def planner_plugin_contract(schema: PluginSchemaInfo) -> PlannerPluginContract:
    """Project one admitted schema into the planner's bounded JIT contract."""
    if type(schema) is not PluginSchemaInfo:
        raise TypeError("schema must be an admitted PluginSchemaInfo")
    _assert_projection_input_bounds(schema.json_schema)
    _assert_projection_input_bounds(schema.knob_schema)
    json_schema = _contract_json_schema(schema.json_schema)
    knob_schema = _contract_knob_schema(schema.knob_schema)
    if isinstance(json_schema, bool):
        raise _SchemaContractProjectionUnsupported
    projected = {
        "plugin_id": f"{schema.plugin_type}/{schema.name}",
        "json_schema": json_schema,
        "knob_schema": knob_schema,
        "composer_hints": list(schema.composer_hints),
    }
    if len(canonical_json(projected).encode("utf-8")) > _PLANNER_CONTRACT_MAX_CANONICAL_BYTES:
        raise _SchemaContractProjectionUnsupported
    contract_shape = {"json_schema": json_schema, "knob_schema": knob_schema}
    return PlannerPluginContract(
        plugin_id=f"{schema.plugin_type}/{schema.name}",
        schema_hash=stable_hash(contract_shape),
        json_schema=json_schema,
        knob_schema=knob_schema,
        composer_hints=tuple(schema.composer_hints),
    )


@trust_boundary(
    tier=3,
    source="Plugin-declared JSON Schema 'discriminator' fragment (raw ConfigModel.model_json_schema() output)",
    source_param="raw",
    suppresses=("R5",),
    invariant=(
        "raises _SchemaContractProjectionUnsupported unless raw is a dict containing only "
        "propertyName (str) and/or mapping (dict[str, str]) keys"
    ),
    test_ref="tests/unit/web/composer/test_schema_contract_projection_boundaries.py::test_contract_discriminator_rejects_unknown_key",
    test_fingerprint="4b5ad940600ffcc92ca815af3d0415e09da1dc9df7104550cae9f0a41be917f6",
)
def _contract_discriminator(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) - {"propertyName", "mapping"}:
        raise _SchemaContractProjectionUnsupported
    projected: dict[str, object] = {}
    property_name = raw.get("propertyName")
    if property_name is not None:
        if not isinstance(property_name, str):
            raise _SchemaContractProjectionUnsupported
        projected["propertyName"] = property_name
    mapping = raw.get("mapping")
    if mapping is not None:
        if not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in mapping.items()):
            raise _SchemaContractProjectionUnsupported
        projected["mapping"] = deepcopy(mapping)
    return projected


@trust_boundary(
    tier=3,
    source="Plugin-declared JSON Schema fragment (raw ConfigModel.model_json_schema() output; recursive)",
    source_param="raw",
    suppresses=("R5",),
    invariant=(
        "raises _SchemaContractProjectionUnsupported on any JSON Schema KEYWORD outside the closed "
        "projected vocabulary, and on any container-valued keyword whose value shape is wrong; the "
        "boolean schema forms (true/false) pass through unchanged. Deliberately NOT claimed: values of "
        "_JSON_SCHEMA_SCALAR_KEYS keywords (type/const/enum/default/pattern/minimum/uniqueItems/...) are "
        "deep-copied through WITHOUT a type check, so e.g. pattern=12345 or minimum='x' is accepted"
    ),
    test_ref="tests/unit/web/composer/test_schema_contract_projection_boundaries.py::test_contract_json_schema_rejects_non_dict_non_bool",
    test_fingerprint="88f4d910a5b39715fd50c60330e587e198eddfef3263c5101ed6461593ef561c",
)
def _contract_json_schema(raw: object) -> dict[str, object] | bool:
    """Project all known JSON Schema semantics while excluding only prose."""
    if isinstance(raw, bool):
        return raw
    if not isinstance(raw, dict):
        raise _SchemaContractProjectionUnsupported
    raw_properties = raw.get("properties")
    hidden_properties: set[str] = set()
    if isinstance(raw_properties, dict):
        hidden_properties = {
            name
            for name, schema in raw_properties.items()
            if isinstance(name, str) and isinstance(schema, dict) and schema.get("composer_hidden") is True
        }
    projected: dict[str, object] = {}
    for key, value in raw.items():
        if key in _JSON_SCHEMA_PROSE_KEYS or key == "composer_required_when":
            continue
        if key == "composer_hidden":
            if not isinstance(value, bool) or value:
                raise _SchemaContractProjectionUnsupported
            continue
        if key == "required":
            if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
                raise _SchemaContractProjectionUnsupported
            projected[key] = [name for name in value if name not in hidden_properties]
        elif key == "dependentRequired":
            if not isinstance(value, dict):
                raise _SchemaContractProjectionUnsupported
            dependent: dict[str, list[str]] = {}
            for name, required_names in value.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(required_names, list)
                    or any(not isinstance(required_name, str) for required_name in required_names)
                ):
                    raise _SchemaContractProjectionUnsupported
                if name not in hidden_properties:
                    dependent[name] = [required_name for required_name in required_names if required_name not in hidden_properties]
            projected[key] = dependent
        elif key in _JSON_SCHEMA_SCALAR_KEYS:
            projected[key] = deepcopy(value)
        elif key in _JSON_SCHEMA_MAP_KEYS and isinstance(value, dict):
            if any(not isinstance(name, str) for name in value):
                raise _SchemaContractProjectionUnsupported
            projected[key] = {
                name: _contract_json_schema(schema)
                for name, schema in value.items()
                if not (key == "properties" and name in hidden_properties)
            }
        elif key in _JSON_SCHEMA_SINGLE_SCHEMA_KEYS:
            projected[key] = _contract_json_schema(value)
        elif key in _JSON_SCHEMA_SCHEMA_LIST_KEYS and isinstance(value, list):
            projected[key] = [_contract_json_schema(branch) for branch in value]
        elif key == "discriminator":
            projected[key] = _contract_discriminator(value)
        else:
            raise _SchemaContractProjectionUnsupported
    return projected


@trust_boundary(
    tier=3,
    source="Plugin-declared ELSPETH knob-schema fragment (PolicyCatalogView.get_schema knob_schema; recursive)",
    source_param="raw",
    suppresses=("R5",),
    invariant=(
        "raises _SchemaContractProjectionUnsupported on any field shape outside the closed "
        "{name,kind,type,required,nullable,default,enum,choices,item_kind,visible_when,required_when,"
        "item_schema,items} plus prose-key vocabulary, or when the top-level shape is not exactly {'fields'}"
    ),
    test_ref="tests/unit/web/composer/test_schema_contract_projection_boundaries.py::test_contract_knob_schema_rejects_missing_fields_key",
    test_fingerprint="7cd806e3678d6f8e15df19954921cf662441218973bd39795dbb74726592442e",
)
def _contract_knob_schema(raw: object) -> dict[str, object]:
    """Project the one-knob schema to executable field facts, excluding UI prose."""
    if not isinstance(raw, dict) or set(raw) != {"fields"}:
        raise _SchemaContractProjectionUnsupported
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list):
        raise _SchemaContractProjectionUnsupported
    fields: list[dict[str, object]] = []
    prose_keys = {"label", "description", "placeholder", "tier"}
    scalar_key_order = ("name", "kind", "type", "required", "nullable", "default", "enum", "choices", "item_kind")
    structural_keys = set(scalar_key_order) | {"visible_when", "required_when", "item_schema", "items"}
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            raise _SchemaContractProjectionUnsupported
        if set(raw_field) - prose_keys - structural_keys:
            raise _SchemaContractProjectionUnsupported
        if not isinstance(raw_field.get("name"), str) or not isinstance(raw_field.get("required"), bool):
            raise _SchemaContractProjectionUnsupported
        if not isinstance(raw_field.get("kind") or raw_field.get("type"), str):
            raise _SchemaContractProjectionUnsupported
        if "kind" in raw_field and not isinstance(raw_field["kind"], str):
            raise _SchemaContractProjectionUnsupported
        if "type" in raw_field and not isinstance(raw_field["type"], str):
            raise _SchemaContractProjectionUnsupported
        if "nullable" in raw_field and not isinstance(raw_field["nullable"], bool):
            raise _SchemaContractProjectionUnsupported
        for enum_key in ("enum", "choices"):
            if enum_key in raw_field and (
                not isinstance(raw_field[enum_key], list) or any(not isinstance(choice, str) for choice in raw_field[enum_key])
            ):
                raise _SchemaContractProjectionUnsupported
        if "enum" in raw_field and "choices" in raw_field and raw_field["enum"] != raw_field["choices"]:
            raise _SchemaContractProjectionUnsupported
        if "item_kind" in raw_field and not isinstance(raw_field["item_kind"], str):
            raise _SchemaContractProjectionUnsupported
        field: dict[str, object] = {key: deepcopy(raw_field[key]) for key in scalar_key_order if key in raw_field}
        if "choices" in field and "enum" not in field:
            field["enum"] = field.pop("choices")
        else:
            field.pop("choices", None)
        for predicate_key in ("visible_when", "required_when"):
            if predicate_key not in raw_field:
                continue
            predicate = raw_field[predicate_key]
            if not isinstance(predicate, dict) or set(predicate) != {"field", "equals"} or not isinstance(predicate["field"], str):
                raise _SchemaContractProjectionUnsupported
            field[predicate_key] = {"field": predicate["field"], "equals": deepcopy(predicate["equals"])}
        if "item_schema" in raw_field:
            field["item_schema"] = _contract_knob_schema(raw_field["item_schema"])
        if "items" in raw_field:
            items = raw_field["items"]
            field["items"] = _contract_json_schema(items)
        fields.append(field)
    return {"fields": fields}


def _schema_pair_label(pair: tuple[str, str]) -> str:
    return f"{pair[0]}/{pair[1]}"


def _schema_evidence_envelope(
    *,
    policy_hash: str,
    snapshot_hash: str,
    schemas: list[_SchemaContractEvidenceEntry],
    omitted: list[_SchemaContractEvidenceOmission],
    omissions_withheld_count: int,
) -> _SchemaContractEvidence:
    """Build an envelope whose byte count includes every emitted field."""
    evidence: _SchemaContractEvidence = {
        "policy_hash": policy_hash,
        "snapshot_hash": snapshot_hash,
        "max_entries": _SCHEMA_EVIDENCE_MAX_ENTRIES,
        "max_omissions": _SCHEMA_EVIDENCE_MAX_OMISSIONS,
        "max_canonical_bytes": _SCHEMA_EVIDENCE_MAX_CANONICAL_BYTES,
        "canonical_bytes_used": 0,
        "schemas": schemas,
        "omitted": omitted,
        "omissions_withheld_count": omissions_withheld_count,
    }
    while True:
        rendered_size = len(canonical_json(evidence).encode("utf-8"))
        if evidence["canonical_bytes_used"] == rendered_size:
            return evidence
        evidence["canonical_bytes_used"] = rendered_size


def build_schema_contract_evidence(
    catalog: PolicyCatalogView,
    *,
    schemas_loaded: frozenset[tuple[str, str]],
    referenced: set[tuple[str, str]],
) -> tuple[_SchemaContractEvidence, frozenset[tuple[str, str]]]:
    """Rehydrate whole current contracts for previously discovered identities.

    ``schemas_loaded`` is historical identity evidence only. Schema bytes are
    always fetched again through the current request's ``PolicyCatalogView``;
    policy rotation, profile projection, schema drift, and unavailability can
    therefore never inherit stale bytes from an earlier tool result.
    """
    snapshot = catalog.snapshot
    available = frozenset((plugin_id.kind, plugin_id.name) for plugin_id in snapshot.available)
    loaded_pairs = frozenset(pair for pair in schemas_loaded if pair[0] in {"source", "transform", "sink"})
    ordered_loaded = sorted(loaded_pairs, key=lambda pair: (pair not in referenced, pair[0], pair[1]))
    # Reserve the digit width of the worst-case withheld count while admitting
    # schemas. Final omission details are admitted separately below, but even
    # if none fit, growing ``omissions_withheld_count`` must not push an
    # otherwise boundary-sized final envelope over the byte cap.
    omission_count_upper_bound = len(referenced - loaded_pairs) + len(ordered_loaded)
    omission_candidates: list[_SchemaContractEvidenceOmission] = [
        {"plugin_id": _schema_pair_label(pair), "reason": "not_loaded_this_session"} for pair in sorted(referenced - loaded_pairs)
    ]
    entries: list[_SchemaContractEvidenceEntry] = []
    evidenced: set[tuple[str, str]] = set()

    for pair in ordered_loaded:
        label = _schema_pair_label(pair)
        if pair not in available:
            # A historical success is not authority to redisclose an identity
            # hidden by the current policy. A referenced identity is already
            # present in current_state, so naming its closed omission adds no
            # new disclosure and keeps the gap actionable.
            if pair in referenced:
                omission_candidates.append({"plugin_id": label, "reason": "unavailable_in_current_policy"})
            continue
        if len(entries) >= _SCHEMA_EVIDENCE_MAX_ENTRIES:
            omission_candidates.append({"plugin_id": label, "reason": "entry_budget_exceeded"})
            continue
        kind = pair[0]
        try:
            schema = catalog.get_schema(kind, pair[1])
        except ValueError:
            omission_candidates.append({"plugin_id": label, "reason": "schema_unavailable"})
            continue
        if schema.plugin_type != kind or schema.name != pair[1]:
            omission_candidates.append({"plugin_id": label, "reason": "schema_identity_mismatch"})
            continue
        try:
            json_schema = _contract_json_schema(schema.json_schema)
            knob_schema = _contract_knob_schema(schema.knob_schema)
        except _SchemaContractProjectionUnsupported:
            omission_candidates.append({"plugin_id": label, "reason": "schema_projection_unsupported"})
            continue
        if isinstance(json_schema, bool):
            omission_candidates.append({"plugin_id": label, "reason": "schema_projection_unsupported"})
            continue
        contract = {"json_schema": json_schema, "knob_schema": knob_schema}
        entry: _SchemaContractEvidenceEntry = {
            "plugin_id": label,
            "policy_hash": snapshot.policy_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "schema_hash": stable_hash(contract),
            "json_schema": json_schema,
            "knob_schema": knob_schema,
        }
        prospective = _schema_evidence_envelope(
            policy_hash=snapshot.policy_hash,
            snapshot_hash=snapshot.snapshot_hash,
            schemas=[*entries, entry],
            omitted=[],
            omissions_withheld_count=omission_count_upper_bound,
        )
        if prospective["canonical_bytes_used"] > _SCHEMA_EVIDENCE_MAX_CANONICAL_BYTES:
            omission_candidates.append({"plugin_id": label, "reason": "canonical_byte_budget_exceeded"})
            continue
        entries.append(entry)
        evidenced.add(pair)

    omitted: list[_SchemaContractEvidenceOmission] = []
    for omission in omission_candidates:
        if len(omitted) >= _SCHEMA_EVIDENCE_MAX_OMISSIONS:
            break
        prospective_omitted = [*omitted, omission]
        prospective = _schema_evidence_envelope(
            policy_hash=snapshot.policy_hash,
            snapshot_hash=snapshot.snapshot_hash,
            schemas=entries,
            omitted=prospective_omitted,
            omissions_withheld_count=len(omission_candidates) - len(prospective_omitted),
        )
        if prospective["canonical_bytes_used"] <= _SCHEMA_EVIDENCE_MAX_CANONICAL_BYTES:
            omitted.append(omission)

    evidence = _schema_evidence_envelope(
        policy_hash=snapshot.policy_hash,
        snapshot_hash=snapshot.snapshot_hash,
        schemas=entries,
        omitted=omitted,
        omissions_withheld_count=len(omission_candidates) - len(omitted),
    )
    if evidence["canonical_bytes_used"] > _SCHEMA_EVIDENCE_MAX_CANONICAL_BYTES:
        raise RuntimeError("schema_contract_evidence_budget_invariant")
    return evidence, frozenset(evidenced)


def _digest_entries(plugins: list[PluginSummary]) -> list[_PluginDigestEntry]:
    """Render one compact selection entry per policy-visible plugin.

    ``not_for`` is the plugin's ``usage_when_not_to_use`` — its own stated
    prohibition, already profile-projected when it reaches this function,
    because an operator-profiled summary rewrites the field in the catalog view
    itself (``plugin_policy.profiles``). It carried
    the ``text`` sink's "not for multiline values" rule that a planner authored
    straight past (elspeth-afdf55a17c), because until now the only tier that
    stated it was a ``list_sinks`` result the planner is told it rarely needs.

    Reference content is carried whole or not at all. A sliced prohibition
    ("Do not use for multi-field, nested, binary, or multi…") reads as a
    narrower rule than the one the plugin declared, and dropping the entry is
    not available either: ``prompts.py`` retired its duplicate ``plugin_hints``
    block on the strength of this digest being a strict superset, so every
    policy-visible plugin must appear.

    ``example_use`` is deliberately not carried. It is YAML, while this surface
    authors through ``set_pipeline``; it is not validated against the live
    catalog, unlike every worked exemplar this module renders; and it is the
    largest of the reference fields. ``usage_when_to_use`` is not carried
    either — ``purpose`` and ``capability_tags`` already say what a plugin is
    for. Both stay reachable through ``list_sources``/``list_transforms``/
    ``list_sinks``, which return the whole ``PluginSummary``.
    """
    entries: list[_PluginDigestEntry] = []
    for plugin in plugins:
        entry: _PluginDigestEntry = {
            "name": plugin.name,
            "purpose": plugin.description,
            "required_options": [field.name for field in plugin.config_fields if field.required],
        }
        # PluginSummary is a strict Tier-1 response model: the catalog service
        # is the boundary that already rejected a non-str prohibition or a
        # non-tuple tag set, so presence is the only question left here.
        if plugin.usage_when_not_to_use is not None:
            entry["not_for"] = plugin.usage_when_not_to_use
        if plugin.capability_tags:
            entry["capability_tags"] = list(plugin.capability_tags)
        entries.append(entry)
    return entries


def discovery_digest(
    catalog: PolicyCatalogView,
    *,
    summaries: Mapping[str, list[PluginSummary]] | None = None,
) -> _DiscoveryDigest:
    """Per-plugin digest of the policy-visible catalog for the planner prompt.

    Targets ``planner_code=DISCOVERY_CYCLE`` churn: a significant share of
    planner calls were ``list_*``/``get_plugin_schema`` rounds re-learning the
    same catalog every session. Each entry carries the plugin's name, one-line
    purpose, required-knob names, its stated prohibition (``not_for``) and
    its ``capability_tags``. This is selection
    and coaching metadata, not the plugin's option contract; types, optional
    knobs, defaults, enums, and conditional rules remain schema facts.

    Because the planner is told it rarely needs ``list_*``, a selection fact
    that lives only in a ``list_*`` result is a fact it will usually not read.
    That is why the prohibition belongs here and not only in the catalog tier.
    """
    if summaries is None:
        summaries = _plugin_summaries(catalog)
    digest: _DiscoveryDigest = {
        "sources": _digest_entries(summaries["source"]),
        "transforms": _digest_entries(summaries["transform"]),
        "sinks": _digest_entries(summaries["sink"]),
    }

    # Every operator-profiled component carries its live alias enum and public
    # required set. Kind-qualified identity prevents same-name source and
    # transform profiles from borrowing each other's contract.
    digest_by_kind: dict[PluginKind, list[_PluginDigestEntry]] = {
        "source": digest["sources"],
        "transform": digest["transforms"],
        "sink": digest["sinks"],
    }
    for plugin_id, aliases in catalog.snapshot.usable_profile_aliases:
        if not aliases:
            continue
        public_schema = catalog.get_schema(plugin_id.kind, plugin_id.name)
        public_required = [field["name"] for field in public_schema.knob_schema.get("fields", ()) if field.get("required")]
        for entry in digest_by_kind[plugin_id.kind]:
            if entry["name"] == plugin_id.name:
                entry["profile_aliases"] = sorted(aliases)
                entry["required_options"] = public_required
                break
    return digest


_DISCOVERY_DIGEST_GUIDANCE: Final[str] = (
    "This digest is rendered from the live policy-visible catalog at prompt "
    "build and is current for this deployment. For plugin selection, plan directly from it; "
    "it is the complete selection index, "
    "not a full option contract: required-option names are incomplete without "
    "types, optional knobs, defaults, enums, and conditional rules. Author "
    "options only from schema_contract_evidence for this request or a current "
    "get_plugin_schema result. Detailed composer hints are disclosed only in "
    "the bounded contract for a chosen plugin. An entry's not_for is that plugin's own stated "
    "prohibition and is binding on selection: when the value you intend to "
    "write matches it, choose a different plugin or reshape the value upstream "
    "first. capability_tags is the plugin's declared capability vocabulary. "
    "Entries carry no worked example; the worked shapes validated for this "
    "deployment are the other authoring_aids sections. "
    "You rarely need list_sources/list_transforms/"
    "list_sinks calls. Model identifiers still come only from "
    "list_models. A list_models result is a session snapshot and can become "
    "stale, so refresh it before binding a literal model; blob/secret "
    "discovery is unchanged. Use "
    "get_plugin_assistance and explain_validation_error for structured "
    "repair when a proposal is rejected."
)


def _llm_source_generation_rules(*, profile_alias: str | None, output_control: str | None) -> list[str]:
    """Source-native LLM guidance for pipelines that begin with generation."""
    rules = [
        "For a generation-first pipeline with no input rows, use source:llm. "
        "Do not fabricate a seed/null row or add an upstream source plus a transform merely to trigger one model call.",
        "One successful authored prompt emits exactly one source row. The text lands in options.response_field "
        "(default llm_response), with <response_field>_usage and <response_field>_model appended automatically.",
        "The prompt is static with respect to pipeline rows: no incoming row exists and {{ row... }} is invalid. "
        "Use options.lookup for explicit authored values and reference them through {{ lookup... }}; Jinja globals are also available.",
        "Prompt Shield is not applicable to this static author-authored source prompt. Content Safety still applies downstream "
        "to the generated row when deployment policy requires it.",
        "Model-catalog results from list_models are a session snapshot and can become stale; refresh before binding a literal "
        "model on a trained-operator/provider-form surface. Never invent or recall a model identifier from training.",
    ]
    if profile_alias is not None:
        rules.append(
            f"This deployment serves source:llm through the operator profile alias '{profile_alias}'. Set options.profile to "
            "that alias and omit provider/model/credential fields; operator policy owns those private bindings."
        )
    else:
        rules.append(
            "No source:llm operator profile is currently usable. On a trained-operator surface, bind a literal model only "
            "from a fresh list_models result; on the web surface, ask the operator to provide a usable profile."
        )
    if output_control is not None:
        rules.append(
            f"This deployment REQUIRES the {output_control} Content Safety control for generated output. Set "
            "options.on_validation_failure to 'discard': a non-discard source validation exit cannot be placed downstream "
            "of the control and is rejected as an uncontrolled output path."
        )
    else:
        rules.append(
            "Set options.on_validation_failure explicitly. Use 'discard' for intentional dropping; a named sink retains "
            "invalid generated rows only when deployment control policy permits that path."
        )
    return rules


def source_custody_exemplar_args(
    catalog: PolicyCatalogView,
    *,
    blob_id: str | None = None,
    visible: Mapping[str, frozenset[str]] | None = None,
) -> _SetPipelineExemplar | None:
    """Complete ``set_pipeline`` args showing one legal source custody binding.

    With ``blob_id=None`` the source binds literal user data via
    ``inline_blob``; passing a ``blob_id`` (the prompt passes
    :data:`PLACEHOLDER_BLOB_ID`; the validation test passes a real created
    blob's id) shows the existing-blob binding instead. Everything outside
    ``source`` is byte-identical between the variants. Returns ``None`` when
    the plugins the exemplar names are not policy-visible. ``visible`` lets
    the payload builder share one catalog sweep across every exemplar.
    """
    if visible is None:
        visible = _visible_plugin_names(catalog)
    if "csv" not in visible["source"] or "json" not in visible["sink"]:
        return None
    source: _ExemplarSource = {
        "plugin": "csv",
        "on_success": "main",
        "options": {"schema": {"mode": "observed"}},
        "on_validation_failure": "discard",
    }
    if blob_id is None:
        source["inline_blob"] = {
            "filename": _INLINE_EXEMPLAR_FILENAME,
            "mime_type": _INLINE_EXEMPLAR_MIME,
            "content": _INLINE_EXEMPLAR_CONTENT,
            "description": "Literal rows the user pasted into chat",
        }
    else:
        source["blob_id"] = blob_id
    exemplar: _SetPipelineExemplar = {
        "source": source,
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {
                    "path": "outputs/stock_levels.json",
                    "format": "json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {
            "name": "Save pasted rows",
            "description": "Bind user-provided rows through blob custody and write them to one JSON output.",
        },
    }
    return exemplar


def _renderable_branch_plugins(transforms: list[PluginSummary]) -> list[str]:
    """Non-LLM transforms authorable with only the universal ``schema`` option.

    The profile-less exemplar variant needs branch transforms it can configure
    generically: a plugin whose required options go beyond ``schema`` would
    demand invented values, and a batch-aware plugin authored as
    ``node_type='transform'`` needs the aggregation path (or extra row-mode
    options) the exemplar cannot generically supply. Sorted so the pick is
    deterministic per snapshot; plugin classes are static per process, so the
    batch-aware sweep cannot drift within a memo entry's lifetime.
    """
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    batch_aware = frozenset(cls.name for cls in get_shared_plugin_manager().get_transforms() if cls.is_batch_aware)
    return sorted(
        plugin.name
        for plugin in transforms
        if plugin.name != "llm"
        and plugin.name not in batch_aware
        and {field.name for field in plugin.config_fields if field.required} <= {"schema"}
    )


def fork_coalesce_exemplar_args(
    catalog: PolicyCatalogView,
    *,
    visible: Mapping[str, frozenset[str]] | None = None,
    summaries: Mapping[str, list[PluginSummary]] | None = None,
) -> _SetPipelineExemplar | None:
    """Complete ``set_pipeline`` args for the fork -> branches -> coalesce shape.

    The operator-ruled A/B topology: a gate fans identical rows out to two
    branches, each branch runs its own transform, a coalesce rejoins them
    under ``require_all``/``union``, one cleanup transform consumes the
    coalesce id, and a sink receives the tidied rows. The WIRING is pure
    topology and renders regardless of LLM availability; only the branch
    contents vary. With a usable llm operator profile the branches are two
    llm transforms (own prompt, own ``response_field``, short-form
    interpretation_requirements); without one the SAME topology renders with
    one policy-visible non-LLM transform under two distinct node ids. Reusing
    one transform/configuration keeps both union branches on the same runtime
    schema mode; ``passthrough`` is preferred when visible, otherwise the
    first alphabetic candidate whose only required option is ``schema`` is
    used. Returns ``None``
    only when the fixed plugins (csv/json/field_mapper) or every branch
    candidate are policy-hidden — an exemplar must never model an invented
    identifier.
    """
    if summaries is None:
        summaries = _plugin_summaries(catalog)
    if visible is None:
        visible = _visible_plugin_names(catalog, summaries)
    if "csv" not in visible["source"] or "json" not in visible["sink"]:
        return None
    if "field_mapper" not in visible["transform"]:
        return None
    profile_alias = _usable_llm_profile_alias(catalog) if "llm" in visible["transform"] else None

    def _branch_llm(
        node_id: str,
        branch: str,
        response_field: str,
        question: str,
        *,
        prompt_template_parts: list[dict[str, Any]] | None = None,
        interpretation_requirements: list[dict[str, Any]] | None = None,
    ) -> _ExemplarNode:
        options: dict[str, Any] = {
            "profile": profile_alias,
            "prompt_template": question,
            "required_input_fields": ["ticket_id", "body"],
            "response_field": response_field,
            "schema": {"mode": "observed"},
            # llm_prompt_template and llm_model_choice reviews are backend
            # auto-staged on every llm node — never hand-authored. Planner-
            # owned kinds (vague_term, registered pipeline_decision,
            # invented_source) ARE authored when the node calls for them:
            # the urgency branch below authors category semantics and must
            # stage its own wired vague_term review (run-3 E6: an exemplar
            # demonstrating an un-reviewed classification was imported as
            # precedent to skip review-staging).
        }
        if prompt_template_parts is not None:
            options["prompt_template_parts"] = prompt_template_parts
        if interpretation_requirements is not None:
            options["interpretation_requirements"] = interpretation_requirements
        return {
            "id": node_id,
            "node_type": "transform",
            "plugin": "llm",
            "input": branch,
            "on_success": f"{response_field}_done",
            "on_error": "discard",
            "options": options,
        }

    _URGENCY_RUBRIC = (
        "urgency categories: blocking = the user cannot work at all; degraded = "
        "work continues with a broken feature; routine = a question or cosmetic issue"
    )

    metadata: _ExemplarMetadata
    if profile_alias is not None:
        branch_nodes: list[_ExemplarNode] = [
            _branch_llm(
                "assess_sentiment",
                "branch_a",
                "sentiment",
                "What is the sentiment of support ticket {{ row.ticket_id }}: {{ row.body }}? Reply with one short phrase.",
            ),
            _branch_llm(
                "assess_urgency",
                "branch_b",
                "urgency",
                # Authored CLASSIFICATION semantics: the category set is the
                # planner's invention, so the vague_term review is staged and
                # wired below — the review-staging pattern in miniature.
                f"Classify the urgency of support ticket {{{{ row.ticket_id }}}}: {{{{ row.body }}}} using these {_URGENCY_RUBRIC}. Reply with the single category word.",
                prompt_template_parts=[
                    {"kind": "text", "text": "Classify the urgency of support ticket {{ row.ticket_id }}: {{ row.body }} using these "},
                    {"kind": "interpretation_ref", "requirement_id": "urgency:assess_urgency"},
                    {"kind": "text", "text": ". Reply with the single category word."},
                ],
                interpretation_requirements=[
                    {
                        "kind": "vague_term",
                        "user_term": "urgency",
                        "draft": _URGENCY_RUBRIC,
                    }
                ],
            ),
        ]
        coalesce_branches = {"branch_a": "sentiment_done", "branch_b": "urgency_done"}
        tidy_mapping = {
            "ticket_id": "ticket_id",
            "body": "body",
            "sentiment": "sentiment",
            "urgency": "urgency",
        }
        metadata = {
            "name": "Per-branch LLM assessment",
            "description": "Fan rows out to one llm transform per branch, rejoin with a coalesce, tidy, and save.",
        }
    else:
        branch_pool = _renderable_branch_plugins(summaries["transform"])
        if not branch_pool:
            return None
        branch_plugin = "passthrough" if "passthrough" in branch_pool else branch_pool[0]
        branch_nodes = [
            {
                "id": "process_branch_a",
                "node_type": "transform",
                "plugin": branch_plugin,
                "input": "branch_a",
                "on_success": "branch_a_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "process_branch_b",
                "node_type": "transform",
                "plugin": branch_plugin,
                "input": "branch_b",
                "on_success": "branch_b_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        ]
        coalesce_branches = {"branch_a": "branch_a_done", "branch_b": "branch_b_done"}
        tidy_mapping = {"ticket_id": "ticket_id", "body": "body"}
        metadata = {
            "name": "Per-branch fan-out and rejoin",
            "description": "Fan rows out to one transform per branch, rejoin with a coalesce, tidy, and save.",
        }
    tidy_fields = list(tidy_mapping.values())
    tidy_schema = {
        "mode": "flexible",
        "fields": [f"{field}: str" for field in tidy_fields],
        "guaranteed_fields": tidy_fields,
    }

    # Required-control coverage is proved per LLM node: the shield must dominate
    # the node's prompt inputs and content safety must dominate every one of its
    # output streams. A forked exemplar that modelled two bare llm branches would
    # therefore teach a topology this deployment's validator rejects, so the
    # controls are wired here whenever the deployment selected one. Placement is
    # chosen so ONE node covers both branches: the shield sits upstream of the
    # fork, content safety downstream of the rejoin. Only the LLM-branch variant
    # needs them — the non-LLM fallback has no LLM node to cover.
    gate_input = "rows"
    tidy_input = "merge_branches"
    control_nodes_before: list[_ExemplarNode] = []
    control_nodes_after: list[_ExemplarNode] = []
    if profile_alias is not None:
        shield_control = _selected_control_profile(catalog, PluginCapability.PROMPT_SHIELD)
        if shield_control is not None:
            shield_plugin, shield_alias = shield_control
            gate_input = "shielded_rows"
            shield_options: dict[str, Any] = {
                # Fields are exactly the branches' prompt inputs.
                "fields": ["ticket_id", "body"],
                "schema": {"mode": "observed"},
            }
            if shield_alias is not None:
                # The operator-owned control binding stays behind the alias.
                shield_options["profile"] = shield_alias
            else:
                shield_options.update(_direct_control_options(summaries, shield_plugin))
            control_nodes_before.append(
                {
                    "id": "shield_ticket_text",
                    "node_type": "transform",
                    "plugin": shield_plugin,
                    "input": "rows",
                    "on_success": "shielded_rows",
                    "on_error": "discard",
                    "options": shield_options,
                }
            )
        safety_control = _selected_control_profile(catalog, PluginCapability.CONTENT_SAFETY)
        if safety_control is not None:
            safety_plugin, safety_alias = safety_control
            tidy_input = "screened_rows"
            safety_options: dict[str, Any] = {
                # Both branches' response fields — one node, both output streams.
                "fields": ["sentiment", "urgency"],
                "schema": {"mode": "observed"},
            }
            if safety_alias is not None:
                # The operator-owned control binding stays behind the alias.
                safety_options["profile"] = safety_alias
            else:
                safety_options.update(_direct_control_options(summaries, safety_plugin))
            if _plugin_declares_field(summaries, safety_plugin, "source"):
                safety_options["source"] = "OUTPUT"
            control_nodes_after.append(
                {
                    "id": "screen_assessments",
                    "node_type": "transform",
                    "plugin": safety_plugin,
                    "input": "merge_branches",
                    "on_success": "screened_rows",
                    "on_error": "discard",
                    "options": safety_options,
                }
            )

    exemplar: _SetPipelineExemplar = {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "schema": {
                    "mode": "flexible",
                    "fields": ["ticket_id: str", "body: str"],
                    "guaranteed_fields": ["ticket_id", "body"],
                }
            },
            "on_validation_failure": "discard",
            "inline_blob": {
                "filename": "support_tickets.csv",
                "mime_type": "text/csv",
                "content": _FORK_EXEMPLAR_CONTENT,
                "description": "Literal rows the user pasted into chat",
            },
        },
        "nodes": [
            *control_nodes_before,
            {
                "id": "fan_out",
                "node_type": "gate",
                "input": gate_input,
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": ["branch_a", "branch_b"],
            },
            *branch_nodes,
            {
                "id": "merge_branches",
                "node_type": "coalesce",
                # input is schema-required but not a consuming binding for a
                # coalesce (consumption is the branches values) — first
                # branch's arriving connection, by convention.
                "input": next(iter(coalesce_branches.values())),
                "branches": coalesce_branches,
                "policy": "require_all",
                "merge": "union",
                "options": {"schema": {"mode": "observed"}},
            },
            *control_nodes_after,
            {
                "id": "tidy_columns",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": tidy_input,
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "schema": tidy_schema,
                    "mapping": tidy_mapping,
                    "select_only": True,
                },
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {
                    "path": "outputs/ticket_assessments.json",
                    "format": "json",
                    "schema": {"mode": "fixed", "fields": [f"{field}: str" for field in tidy_fields]},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": metadata,
    }
    return exemplar


def fork_row_union_exemplar_args(
    catalog: PolicyCatalogView,
    *,
    visible: Mapping[str, frozenset[str]] | None = None,
) -> _SetPipelineExemplar | None:
    """Complete ``set_pipeline`` args for forked rows released by row_union."""
    if visible is None:
        visible = _visible_plugin_names(catalog, _plugin_summaries(catalog))
    if (
        "csv" not in visible["source"]
        or "json" not in visible["sink"]
        or "passthrough" not in visible["transform"]
        or "field_mapper" not in visible["transform"]
    ):
        return None

    branches = {"branch_a": "branch_a_done", "branch_b": "branch_b_done"}
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "schema": {
                    "mode": "flexible",
                    "fields": ["case_id: str", "variant_text: str"],
                    "guaranteed_fields": ["case_id", "variant_text"],
                }
            },
            "on_validation_failure": "discard",
            "inline_blob": {
                "filename": "experiment_variants.csv",
                "mime_type": "text/csv",
                "content": _ROW_UNION_EXEMPLAR_CONTENT,
                "description": "Literal rows the user pasted into chat",
            },
        },
        "nodes": [
            {
                "id": "fan_out_variants",
                "node_type": "gate",
                "input": "rows",
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": list(branches),
            },
            {
                "id": "process_control",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_a",
                "on_success": "branch_a_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "process_treatment",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "branch_b",
                "on_success": "branch_b_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
            {
                "id": "variant_union",
                "node_type": "row_union",
                "input": next(iter(branches.values())),
                "branches": branches,
                "on_success": "unioned_rows",
                "timeout_seconds": 30.0,
            },
            {
                "id": "tidy_unioned_rows",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "unioned_rows",
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "schema": {
                        "mode": "flexible",
                        "fields": ["case_id: str", "variant_text: str"],
                        "guaranteed_fields": ["case_id", "variant_text"],
                    },
                    "mapping": {"case_id": "case_id", "variant_text": "variant_text"},
                    "select_only": True,
                },
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {
                    "path": "outputs/row_union_variants.json",
                    "format": "json",
                    "schema": {"mode": "fixed", "fields": ["case_id: str", "variant_text: str"]},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {
            "name": "Fork and release row variants",
            "description": "Fan rows through two independent branches, then release every correlated row with row_union.",
        },
    }


# Payload memo keyed by plugin-policy snapshot hash. The aids depend only on
# the policy-visible catalog projection (plugin classes are static per
# process; visibility and profile aliases are exactly what the snapshot hash
# covers), and a cold build costs a full catalog sweep (~50ms) — too much to
# repeat inside every planner call's wall-clock budget. Bounded so snapshot
# rotation cannot grow it without limit; callers receive a deep copy so no
# caller can poison the cached payload.
_AIDS_MEMO: dict[str, _PlannerAuthoringAids] = {}
_AIDS_MEMO_MAX: Final[int] = 8
_AIDS_MEMO_LOCK = Lock()


def build_planner_authoring_aids(catalog: PolicyCatalogView) -> _PlannerAuthoringAids:
    """Assemble the live authoring-aids payload for one planner call.

    Rendered from the policy-visible catalog (memoized per snapshot hash), so
    it can never drift from the deployment. Sections whose plugins are
    policy-hidden are omitted rather than rendered with invented names.
    """
    key = catalog.snapshot.snapshot_hash
    with _AIDS_MEMO_LOCK:
        cached = _AIDS_MEMO.get(key)
    if cached is None:
        built = _build_planner_authoring_aids(catalog)
        with _AIDS_MEMO_LOCK:
            # Another worker may have populated this snapshot while the
            # catalog sweep ran. Prefer that value and mutate/evict only while
            # holding the cache lock.
            cached = _AIDS_MEMO.get(key)
            if cached is None:
                if len(_AIDS_MEMO) >= _AIDS_MEMO_MAX:
                    _AIDS_MEMO.pop(next(iter(_AIDS_MEMO)))
                _AIDS_MEMO[key] = cached = built
    return deepcopy(cached)


def _build_planner_authoring_aids(catalog: PolicyCatalogView) -> _PlannerAuthoringAids:
    summaries = _plugin_summaries(catalog)
    visible = _visible_plugin_names(catalog, summaries)
    aids: _PlannerAuthoringAids = {
        "purpose": (
            "Server-rendered worked exemplars and catalog digest from the live "
            "policy-visible catalog. These shapes validate against the current deployment."
        ),
    }
    custody = source_custody_exemplar_args(catalog, visible=visible)
    custody_blob_variant = source_custody_exemplar_args(catalog, blob_id=PLACEHOLDER_BLOB_ID, visible=visible)
    if custody is not None and custody_blob_variant is not None:
        aids["source_custody"] = {
            "rules": list(_SOURCE_CUSTODY_RULES),
            "set_pipeline_exemplar_inline_blob": custody,
            "existing_blob_source_binding": custody_blob_variant["source"],
        }
    fork_coalesce = fork_coalesce_exemplar_args(catalog, visible=visible, summaries=summaries)
    if fork_coalesce is not None:
        aids["fork_coalesce"] = {
            "rules": list(_FORK_COALESCE_RULES),
            "set_pipeline_exemplar": fork_coalesce,
        }
    fork_row_union = fork_row_union_exemplar_args(catalog, visible=visible)
    if fork_row_union is not None:
        aids["fork_row_union"] = {
            "rules": list(_FORK_ROW_UNION_RULES),
            "set_pipeline_exemplar": fork_row_union,
        }
    # Resolved before the llm aids because the on_error rule they carry is
    # control-mode conditional. ``_selected_control_profile`` returns None for
    # recommend mode or no selection, so this is the same gate the
    # content_safety aid uses. Memo safety: control_modes feeds
    # PluginAvailabilitySnapshot's canonical payload and therefore
    # snapshot_hash, which keys _AIDS_MEMO — one deployment's posture can never
    # be served to another.
    required_safety = _selected_control_profile(catalog, PluginCapability.CONTENT_SAFETY)
    required_output_control = required_safety[0] if required_safety is not None else None
    # Auto-wirability mirrors required_controls.wire_required_controls exactly:
    # an alias-backed selection, or a direct-config selection whose required
    # bindings are all real (never placeholder exemplars), is auto-wired; the
    # aids must not claim the guarantee for any other posture.
    required_shield = _selected_control_profile(catalog, PluginCapability.PROMPT_SHIELD)
    shield_auto_wired = required_shield is not None and (
        required_shield[1] is not None or _direct_control_options_are_deployable(summaries, required_shield[0])
    )
    safety_auto_wired = required_safety is not None and (
        required_safety[1] is not None or _direct_control_options_are_deployable(summaries, required_safety[0])
    )
    if "llm" in visible["transform"]:
        aids["model_custody"] = {
            "rules": _model_custody_rules(_usable_llm_profile_alias(catalog)),
        }
        aids["llm_output_contract"] = {"rules": _llm_output_contract_rules(output_control=required_output_control)}
    if "llm" in visible["source"]:
        aids["llm_source_generation"] = {
            "rules": _llm_source_generation_rules(
                profile_alias=_usable_llm_profile_alias(catalog, kind="source"),
                output_control=required_output_control,
            )
        }
    aids["review_registry"] = {
        # Imported from interpretation_state so the taught vocabulary can
        # never drift from the resolve-time registry (52322ebe1 discipline).
        "registered_pipeline_decision_user_terms": sorted(REGISTERED_PIPELINE_DECISION_USER_TERMS),
        "rules": list(_REVIEW_REGISTRY_RULES),
    }
    visible_untrusted_producers = tuple(sorted(_UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS & visible["transform"]))
    visible_raw_html_producers = tuple(sorted(_RAW_HTML_CLEANUP_PRODUCER_PLUGINS & visible["transform"]))
    control_modes = dict(catalog.snapshot.control_modes)
    if visible_untrusted_producers and "llm" in visible["transform"]:
        aids["prompt_shield"] = {
            "rules": _prompt_shield_rules(
                # Whichever shield THIS deployment selected (aws_bedrock_prompt_shield,
                # azure_prompt_shield, …) — never a hardcoded vendor.
                shield_plugin=(
                    selected_shield.name
                    if (selected_shield := dict(catalog.snapshot.selected).get(PluginCapability.PROMPT_SHIELD))
                    else None
                ),
                shield_required=control_modes.get(PluginCapability.PROMPT_SHIELD, ControlMode.RECOMMEND) is ControlMode.REQUIRED,
                shield_auto_wired=shield_auto_wired,
                untrusted_producers=visible_untrusted_producers,
            ),
        }
    if "llm" in visible["transform"] and required_output_control is not None:
        aids["content_safety"] = {"rules": _content_safety_rules(safety_plugin=required_output_control, auto_wired=safety_auto_wired)}
    if visible_raw_html_producers and "field_mapper" in visible["transform"]:
        aids["raw_html_cleanup"] = {"rules": _raw_html_cleanup_rules(untrusted_producers=visible_raw_html_producers)}
    if "web_scrape" in visible["transform"]:
        aids["web_scrape_http_identity"] = {"rules": list(_WEB_SCRAPE_HTTP_IDENTITY_RULES)}
    aids["discovery_digest"] = {
        "guidance": _DISCOVERY_DIGEST_GUIDANCE,
        "plugins": discovery_digest(catalog, summaries=summaries),
    }
    return aids


__all__ = [
    "PLACEHOLDER_BLOB_ID",
    "PlannerPluginContract",
    "SchemaContractProjectionUnsupported",
    "build_planner_authoring_aids",
    "build_schema_contract_evidence",
    "discovery_digest",
    "fork_coalesce_exemplar_args",
    "fork_row_union_exemplar_args",
    "planner_plugin_contract",
    "source_custody_exemplar_args",
]
