# Composer architectural synthesis

Baseline: `3b28d537be98234da8135c418bf185a998c18b68`, restored 0.8.9.
Scope is reconnaissance and remediation design, not production implementation or
release qualification. The [live inventory](inventory.md) reconciles 46 distinct
Web Composer tool names. Each has its own [assessment sheet](inventory.md#tool-by-tool-coverage).

## Assessment

The strongest intervention is to make invocation context, observations and
effect settlement explicit contracts across the existing Composer paths.
The code already has substantial orchestration, policy and integrity machinery;
the evidence does not support replacing it with a new general agent framework.
The failures below occur where routes assemble different optional dependencies,
where a result does not explain the completeness or meaning of its observation,
or where a tool's graph/result category is an incomplete description of its
lifecycle. Individual fixes are useful containment; the intended solution is
shared ownership of these boundaries with executable route-level contracts.

The orchestrator must reliably distinguish “not present” from “not discovered,”
“tool returned” from “effect committed,” “version advanced” from “useful change,”
and “editable draft recovered” from “pipeline work complete.” Plugins must own
configuration and execution semantics; tool wrappers and policy must preserve
and truthfully project those semantics rather than independently recreate them.

## Existing foundations to retain

- **Provider authorship and audited call ownership.** The
  [planner](../../../src/elspeth/web/composer/pipeline_planner.py#L1605) and
  [freeform gateway](../../../src/elspeth/web/composer/provider_gateway.py#L414)
  pin SDK retries to zero and keep physical call accounting in Composer.
  Recovery also calls the provider. No recommendation permits server-generated
  proposal topology or a tutorial-special path.
- **Canonical argument admission and wire adaptation.**
  [Tool declarations](../../../src/elspeth/web/composer/tools/declarations.py)
  and the [dispatcher](../../../src/elspeth/web/composer/tools/_dispatch.py#L639)
  validate the semantic contract. The
  [wire projection](../../../src/elspeth/web/composer/tools/wire_projection.py)
  carries transport differences without making provider strictness the only
  validator. This is a useful seam to extend, not discard.
- **Plugin and policy authority.** The
  [catalogue builds schemas from plugin-owned emission](../../../src/elspeth/web/catalog/service.py#L278),
  and [PolicyCatalogView](../../../src/elspeth/web/catalog/policy_view.py#L191)
  exposes policy/profile projections and shared authored/executable validation.
  A policy-hidden or credential-unavailable plugin is not automatically a
  defective plugin. The projection is not universal: profiled `llm`/Bedrock
  assistance falls through unprojected (`get_plugin_assistance-N5`) and the
  validation-suggestion route calls the raw hook (`get_plugin_assistance-N7`,
  latent: no profiled publisher today).
- **Durable publication and recovery authority.**
  [Pipeline commit bindings](../../../src/elspeth/web/composer/pipeline_commit.py)
  and [session turn settlement](../../../src/elspeth/web/sessions/composer_turn.py#L1051)
  protect evidence, base identities and actual terminal outcomes. Focused
  recovery tests demonstrate malformed-head isolation, missing-target refusal,
  cancellation evidence and write-free completed replay.
- **Shared execution.** The
  [web execution service](../../../src/elspeth/web/execution/service.py#L4188)
  calls the runtime orchestrator; Composer authoring is not an alternate
  pipeline executor.

## 1. Invocation dependencies are caller convention rather than a complete tool contract

**Concrete finding:** [GS-01, `diff_pipeline`](tools/diff_pipeline.md).
The public freeform call to
[`execute_tool`](../../../src/elspeth/web/composer/tool_batch.py#L3008) omits
`baseline`; the [dispatcher](../../../src/elspeth/web/composer/tools/_dispatch.py#L795)
defaults it to `None`; the diff handler then refuses with
`diff_baseline_unavailable`. MCP passes a session baseline (rebased even after a
failed `load_session`, `diff_pipeline-N2`). The planner passes
`baseline=current_state` and diffs that same state, so every planner call
returns zero changes (`diff_pipeline-N1`). Controlled handler cases distinguish
missing baseline, a changed baseline and the identical state. The probe wave
then ran all three routes: a real planner loop with a scripted provider returned
zero changes while a mutant binding another baseline returned one; a freeform
service compose refused, and the refusal's synthetic error later appeared in a
successful mutation's `resolved_errors` (`diff_pipeline-N5`, XC-20); and the
MCP handler rebased after a failed load (`diff_pipeline-N2`). These are
scripted-provider runs, not a live model or deployed route.

**Related evidence:** [source inspection](tools/inspect_source.md) can succeed
under a direct context that lacks the storage root and operation authority
required by `get_blob_content`. It also bypasses `_locked_blob_row_for_read`,
whose docstring requires every content read to run inside the custody lock
(`inspect_source-N2`). This proves tool admission inconsistency; it
does not establish a reachable unauthorized HTTP read or a concurrency exploit.
The probe wave demonstrated the crash-leftover consequence (inspection reads an
unhealed pre-update sidecar that `get_blob_content` restores); the concurrent
race was not runnable.

**Underlying cause:** [ToolContext](../../../src/elspeth/web/composer/tools/_common.py#L3678)
is a broad optional-dependency carrier, while
[ToolDeclaration](../../../src/elspeth/web/composer/tools/declarations.py#L122)
does not enforce a complete invocation dependency contract. A route can expose
a tool while silently failing to supply a meaningful prerequisite.

**Orchestrator consequence:** choosing a correctly advertised tool can consume
a turn on a failure the model cannot repair. Route-specific context also makes
tool interchangeability and audit expectations difficult to reason about.

**Owner and direction:** Composer invocation assembly owns explicit capabilities
and required dependencies, checked before exposure/dispatch. Each tool owns its
specific preconditions. Keep transport/session differences explicit; unifying
contracts does not mean advertising every tool on every route.

## 2. Discovery lacks a consistently truthful observation contract

**Concrete findings:** [BOS-01, `list_blobs`](tools/list_blobs.md) and
[`list_composer_blobs`](tools/list_composer_blobs.md) silently cap at 50 newest
rows. Controlled 1/50/51-row cases prove the omitted oldest entry and absent
continuation/completeness signal. No data loss or authorization failure is
implied. [GS-03, `list_models`](tools/list_models.md) and
[`get_audit_info`](tools/get_audit_info.md) have browser descriptions broader
than their implementations, rendered on three surfaces (settled ribbon, info
bubble and live tool log, not only a tooltip): a model catalogue is not a deployment credential
probe; constant audit-policy facts are not current-session run lineage.

**Related source limitation:** recovery context advertises at most 100 parked
graphs ([query](../../../src/elspeth/web/sessions/service.py#L6641)). Older known
IDs can still be restored; the >100-park experience was not exercised.

**Underlying cause:** discovery response contracts do not consistently express
completeness, scope, freshness and the distinction between catalogue facts,
policy eligibility and actual resource readiness. Descriptions are authored in
separate model/UI surfaces without an equally strong semantic parity check.

**Orchestrator consequence:** absence can be mistaken for impossibility; repeated
queries cannot discover hidden records; users may believe a credential/resource
has been checked when only a catalogue was read.

**Owner and direction:** services own scoped observations and paging; tools own
typed result semantics; the orchestrator follows continuation and preserves
uncertainty; UI copy describes the same facts. Do not make discovery perform
unrequested remote probes merely to satisfy an overbroad description.

## 3. Resource admission and owned-storage failure semantics diverge

**Concrete findings:** [BOS-03, `get_blob_metadata`](tools/get_blob_metadata.md)
admits syntactically valid alternative UUID forms but performs a raw-text
lookup. The canonical ID succeeds and the uppercase representation of the same
identity reports missing. Other blob paths canonicalize the lookup but not
every later use: content readers echo the raw selector, proposal custody and
the delete/update retention guards compare raw text, and planner keys split by
spelling (BOS-03, XC-08).
[BOS-02, `get_blob_content`](tools/get_blob_content.md) and
[`read_attachment`](tools/read_attachment.md) return ordinary failed tool
results for missing bytes behind ready metadata, while the canonical blob
service raises `BlobContentMissingError` under the same fence.

**Underlying cause:** wrappers partly reconstruct blob lookup/read/error
contracts instead of consuming one owned resource admission/read interface.
This is separate from legitimate parsing of untrusted source data. Missing own
bytes and malformed external CSV have different owners and recovery actions.

**Orchestrator consequence:** a valid identity may appear nonexistent; an owned
storage failure can look like a user/model-correctable authoring error. Retrying
the same tool or changing the pipeline cannot repair missing service bytes.

**Owner and direction:** the blob domain owns canonical IDs, fenced snapshot
reads, integrity and categorical operational faults. Tool adapters project
safe details and permitted recovery actions. Neither the model nor a downstream
plugin should repair or default corrupted service-owned evidence.

**Additional limitation:** paginated content/preview paths can read/hash whole
files before returning a bounded slice. This is source-established I/O behavior,
not a measured latency, memory exhaustion or deployed denial-of-service claim.
Any optimization must retain exact snapshot/hash/credential safeguards.

## 4. Graph version and handler success are incomplete progress/effect signals

**Concrete observation:** [SG-03, `set_metadata`](tools/set_metadata.md) and
[`patch_node_options`](tools/patch_node_options.md) can advance state version on
empty patches with identical content, while
[`patch_source_options`](tools/patch_source_options.md) returns a no-op without
advancement. The shared
[progress predicate](../../../src/elspeth/web/composer/discovery_cache.py#L38)
then reports progress only for the version-advancing cases.

**Related limitation:** [BOS-04, `create_blob`](tools/create_blob.md),
[`update_blob`](tools/update_blob.md), and [`delete_blob`](tools/delete_blob.md)
have real store effects without graph version advancement. Their durable
custody machinery is valuable. The proposal-accept route already has
receipt-backed replay for blob proposals ([`delete_blob`](tools/delete_blob.md) §5 BOS-04); a fresh
freeform `auto_commit` call does not carry a generally advertised exactly-once
replay identity. This is not evidence that completed operation replay
duplicates a blob or that durable drivers lack recovery.

The [result envelope](../../../src/elspeth/web/composer/tools/_common.py#L993),
[prospective effect domains](../../../src/elspeth/web/composer/tools/declarations.py#L106),
audit dispatch status, proposal staging and final session settlement describe
different facts. The code already separates some of them; a single Boolean or
graph version cannot safely stand in for the others.

**Underlying cause:** progress, validation, proposal and external-effect states
have no single tool receipt vocabulary across routes. New callers must know
which private branch has completed which obligation.

**Orchestrator consequence:** empty work can appear productive, real work can
leave graph version unchanged, and retry decisions require bespoke knowledge.
No-op churn is a demonstrated inconsistency; broad nontermination, data loss or
duplicate-effect claims remain unproven.

**Owner and direction:** tools report graph-content changes and observed effects;
the orchestrator owns workflow progress/retry/terminal decisions; Sessions and
resource services own durable receipts. Preserve all existing fencing/audit
boundaries while making these facts explicit.

## 5. Editing semantics and validity requirements need a common explainable contract

**Concrete defect:** [SG-07, `upsert_node`](tools/upsert_node.md) accepts collector
scope fields on a queue, then drops them while building `NodeSpec` before the
queue invariant runs. Public schema-admitted dispatch reports success, while
the authoritative invariant rejects a node retaining each same field. The
reviewer and parent independently reproduced this with a canonical queue
positive control. This is argument loss before validation, not evidence that
an invalid queue is stored or executed as a collector. Bulk `set_pipeline`
refuses the same input, so SG-07 is specific to `upsert_node`; it is one cell
of a wider wrong-kind admission matrix (`upsert_node-N2`).

**Other findings/limitations:** [SG-02, `splice_transform`](tools/splice_transform.md)
works on the valid control graph but refuses a new insertion when unrelated
pre-existing missing-output findings leave the full candidate invalid (replay
skips the check). The full-validity precondition is not adequately explained in
its selection guidance, and the refusal forwards zero entries while its
guidance says the entries name each new error (`splice_transform-N1`).
Other incremental tools can preserve invalid drafts for later repair.
[SG-01, `upsert_edge`](tools/upsert_edge.md) and [`remove_edge`](tools/remove_edge.md)
also have deliberately different semantics for visual edges and executable
destination scalars. Their restrictions are not proof of incorrect execution,
but the planner must understand which representation is authoritative.

**Underlying cause:** admitted authored facts must survive construction until
their owning invariant validates them; a generic model followed by a partial
kind-specific builder does not ensure that. A tool's editing primitive also
does not consistently declare
whether it supports invalid drafts, requires a fully valid resulting graph,
changes executable routing, or only changes its rendered projection. Plugin
configuration validation, policy lowering, required controls, semantic graph
validation and runtime preflight are legitimate separate stages, but their
obligations must be named consistently.

**Orchestrator consequence:** a correct local edit can fail for unrelated work,
and a successful visual edit need not implement the intended runtime route.
Planning and repair therefore require tool-specific knowledge beyond the graph
shape presented to the model.

**Owner and direction:** Composer semantic graph admission owns authoritative
edit operations and allowed intermediate validity; rendering derives its view.
Plugins own option/capability/execution semantics. Web policy owns permitted
profiles, secrets and required controls. Preserve those distinctions rather than
putting deployment policy into plugin schemas or teaching every handler another
independent copy of runtime rules.

## 6. Responses, control tools and user presentation need one lifecycle vocabulary

**Concrete finding:** [GS-02, `explain_validation_error`](tools/explain_validation_error.md)
promises a matched `error_code`, but its exact-match branch omits the field that
the fuzzy branch supplies. This is small in isolation and illustrates the
benefit of executable branch-complete response contracts over description-only
promises. The UI mismatches in group 2 are a second presentation surface.

**Structural observation:** async advisor and interpretation tools, recovery
controls and the planner terminal live outside the synchronous declaration
universe. This is deliberate current implementation, not by itself a defect.
It means inventory, exposure, argument admission, budgets, audit and completion
have several owners to reconcile. The recovered-state receipts demonstrate
that these paths can be made precise without forcing every operation into a
synchronous mutation handler.

Async tools are not effect-free: in an opted-out session
`request_interpretation_review` appends composition state, and the turn then
fails its own persistence check (`request_interpretation_review-N1`, XC-17).
"Pending review is not a mutation" holds only when review is enabled.

**Orchestrator consequence:** the model's terminal call, advisor stop, pending
review, user handoff and committed pipeline completion must remain different
outcomes. A first-class orchestrator needs those distinctions as owned data,
not only branch-specific control flow or prose.

**Owner and direction:** extend the existing descriptor/result mechanisms to
cover sync, async and terminal roles; retain specialized execution owners and
receipts. Descriptions and UI summaries should consume or be checked against
these contracts. This is an incremental unification of authority, not a demand
for one giant handler or one universal state store.

## 7. Recurring causes newly evidenced by the 2026-11-09 sheet review

The 46 per-sheet reviews (method and limits in
[evidence](evidence.md#per-sheet-review-wave-2026-11-09)) mostly add instances
to groups 1–6. Eight causes recur often enough to name separately. Register
IDs are in the [README](README.md#cross-cutting-findings-added-by-the-review-and-probe-waves).
The probe wave's corrections to these paragraphs are folded in; its new causes
are in section 8.

**One skill text, several palettes.** The planner renders the freeform
composer skill as its own (`rendered_skill = self._composer_skill_text`,
[planning_application.py L1077](../../../src/elspeth/web/composer/planning_application.py#L1077)),
plus authoring aids, plugin `composer_hints` and catalogue repair text written
with the freeform palette in mind. The planner's 23-tool palette lacks every
mutation, so guidance that is correct on freeform is a dead end there (XC-01).
The same mechanism runs in reverse (freeform is taught a planner-only manifest)
and sideways (MCP serves hints naming tools it lacks). The cause is that
guidance is authored per document, not rendered per route from the palette;
nothing tests the two together. Group 2's "descriptions authored in separate
surfaces" is the same cause one level down.

**Publication without admission.** In explicit approval the dry run is gated
on `tool_name in _MUTATION_TOOLS and tool_name != "set_pipeline"`
([tool_batch.py L2057](../../../src/elspeth/web/composer/tool_batch.py#L2057));
the only handled outcome is a valid, version-advancing preview, and a refused
preview has no branch. Blob and secret mutations are outside the set. A
proposal therefore reaches the user while the model is told it succeeded
(XC-02). The probe wave drove staging and then the REST accept route for the
tools XC-02 names and found three accept outcomes, not one: a refused preview gives 422
and auto-reject; a no-op preview gives 409 "did not change composition state"
and leaves the proposal pending indefinitely; a preview `ToolArgumentError`
gives an unhandled 500 and leaves it pending. Staging itself can raise a bare
`ValueError` for an unknown or non-canonical blob id, ending the turn with no
tool row. The approval path is a second dispatcher that does not share the
immediate path's admission, and the accept route is a third.

**Every re-invocation assembles its own context.** Group 1's optional
`ToolContext` reappears at each site that calls `execute_tool` again: the
explicit-approve preview passes `secret_wiring_policy`, the accept route
([proposals.py L493](../../../src/elspeth/web/sessions/routes/composer/proposals.py#L493))
does not, so every accepted secret wire is denied (XC-03, demonstrated through
the accept route with a mutation control); MCP passes `data_dir=None` with
opposite effects on source, sink and provider paths (XC-10). The probe wave
narrowed XC-10: the stdio server's runtime preflight refuses the stored
out-of-root paths at preview and the YAML export omits them, so the effect is a
false "valid" during authoring, not a confinement escape. MCP also skips discovery response admission because its dispatcher
serializes `result.to_dict()` directly
([server.py L415](../../../src/elspeth/composer_mcp/server.py#L415); XC-09).
GS-01 was the first instance, not an isolated one.

**Fault taxonomy fails in both directions.** Group 3 described owned faults
softened into ordinary failures (BOS-02, now on more readers). The reviews
show the inverse as often: Tier-3 values (non-string or NUL paths, NUL
filenames, malformed recovery controls) and expected lifecycle states (a
`pending` blob given to `update_blob`, ESTALE) escape as raw exceptions that end
the turn, and a public wire can write a shape that later trips a Tier-1 guard
in an unrelated tool (XC-05). Web-owned gates dereference values before plugin
validation types them.

**Identity is admitted at one stage and compared raw at another.** Blob and
park IDs are parsed with `UUID()` for admission, then compared as text in
lookup, echo, proposal custody, retention guards and planner keys (BOS-03,
XC-08, XC-13). The integrity-relevant case is the retention guard, not the
lookup miss.

**Dead policy branches are still taught.** `prohibited` has had no producer
since `e7f6f1521` (XC-11, empty across a five-cell policy matrix with a
producer-mutant control); `DISCOVERY_NO_GAIN` and `covers()` have no
production caller (XC-12); the `model_catalog` aid both skills cite is never
attached (`list_models-N6`). The probe wave found a neighbouring case of a
setting that reads as live: `composer_max_discovery_turns` caps only the
planner, freeform has no discovery cap (a test pins that as intended), and the
config validator still adds it to "configured turns". Cached and uncached
freeform rereads both ran to the compose deadline. Each is described in tool descriptions or skills as
live behaviour. When a branch was removed or never wired, its teaching text,
contracts and tests were not removed with it.

**Validity and reconciliation are evaluated by different readers.** Composer
Stage-1 validity reads routing fields kind-blind while lowering drops some
(`upsert_node-N1`); edge/scalar mirroring and the literal `"source"` endpoint are
reconciled at some writers and not others (XC-15, XC-16). An incremental
`is_valid=True` is therefore not a runtime promise, which group 5 implied but
did not demonstrate.

**Instruments that pass for the wrong reason.** Tests or checks were shown to
stay green over the defect they appear to guard: the grammar operator check
uses substring membership, and a coordinator mutation probe confirmed it passes
with `<` and `>` removed (`get_expression_grammar-N4`); a builder-count test
patches a name its caller does not bind, and the probe wave ran it green while
both bindings each counted one build (`set_pipeline-N8`); guidance tests pin the
inverted `explain_validation_error` pointer (`explain_validation_error-N1`);
disabling the metadata credential-refusal branch left both test files that name
the tool green (`get_blob_metadata-N4`). Harness defaults hide behaviour too: the
shared `composer_service_with_real_sessions` fixture wires no `session_engine`,
so `create_blob` through it creates nothing, and `StaticPluginSnapshotFactory`
drops `control_modes`, hiding accept-time control wiring
([`create_blob`](tools/create_blob.md) §6.1; [`set_source_from_blob`](tools/set_source_from_blob.md) §7).
Remediation acceptance should include a mutation control for each new check.

Audit-facing prose has drifted too: several redaction justifications cite a
`tools.py` module that no longer exists, four handler docstrings are dead code
(`get_audit_info-N1`), and `get_tool_definitions` says it returns 42 tools
against 43 measured. This is hygiene, not an agent-facing defect.

## 8. Causes newly evidenced by the 2026-11-09 probe wave

The probe wave ran the review wave's proposed probes (outcomes and network
record in [evidence](evidence.md#probe-wave-outcomes-2026-11-09)). Most runs
confirmed an existing row at a stronger level; eight refuted a claim. Three
causes are new.

**Server-owned steps change the edit without saying so.** Required-control
finalization re-splices a control the model removed and leaves the model's
`applied_component` echo showing the pre-finalization wiring (XC-21,
`remove_node-N5`); after controls are wired around a spliced node, an identical
replay is refused instead of reported as already applied (`splice_transform-N9`).
The approval surface has the same gap for the user: proposal cards fall back to
"Apply composer tool X." and do not show moved routes or the controls accept
will add (XC-22). Review evidence is reconciled on some writers only, so a queue
insertion through `upsert_node` can wedge a draft behind
`review_reconciliation_failed`, and a rebind can carry an acknowledgement to a
different file (XC-23). The cause is that these steps belong to the server, but
their results are not part of the tool outcome or the proposal obligations
(groups 4 and 6).

**Validation state crosses tool boundaries.** A failed discovery's synthetic
validation becomes the next mutation's prior validation, so the mutation reports
the discovery refusal as "resolved" (XC-20); the planner is told to pass a
requirement code its feedback never carries (XC-24). The loop keeps one
`last_validation` for both observations and mutations.

**Audit records what the tool returned; the model sometimes sees more.** A
freeform cache hit records `{success, data, cache_hit}` while the model receives
the rebuilt validation and version (XC-25). The general claim that audit loses
what the model saw was refuted: a private role=audit evidence row keeps the
served result for ordinary calls (`get_pipeline_state-N2`). The sheets that
inferred audit loss from the redacted chat row have been re-read against that
private row: a fix-up run showed it keeps the `outbound_call_made` key that
`request_advisor_hint-N3`'s chat row collapses (other N3 outcomes unchecked);
`remove_node-N11` had already found the result retained there; the incidental
`upsert_node` note now says the private row was not read.

Two of the eight refutations touched claims the shared documents relied on: the
`clear_source` pending-review residual (`clear_source-N4`; pending session events
are retired on save) and the audit half of `get_pipeline_state-N2`. The other six
refuted single-sheet claims (`list_sources-N2` mirror, `patch_node_options-N10`,
`patch_source_options-N5` audit loss, METADATA-N6(e), `set_pipeline-N5` as first
stated, and a `set_source` provider-path sibling). Two shared claims were
corrected rather than refuted: the cause of XC-12 (no freeform cap, not
uncharged cache hits) and the severity of XC-10 (preview refuses the paths).

## Confidence, risk, gaps and follow-ups

Confidence is high for the live inventory, source traces and controlled/local
observations above. “Underlying cause” and the proposed target boundaries are
design judgments supported by those findings, not measured production outcomes.
An independently reviewed plan can still be wrong if its changes weaken a
currently working authority seam.

Implementation risk is highest around storage taxonomy, graph mutation,
approval/receipt compatibility and cancellation/fencing. Introduce contracts
around existing behavior before moving owners. A new result field must account
for strict decoders, persisted audit/proposal payloads and old operation replay.
Compatibility is required for current supported public/operator/persistence
surfaces; no blanket old-Composer exception or data reset is assumed here.

The [evidence record](evidence.md) distinguishes executed tests, source proof,
failed probes and outstanding questions. After the probe wave, most register
rows are demonstrated through in-memory runs, scripted-provider compose or
planner turns, the REST accept route driven in process, the MCP `call_tool`
handler or jsdom; 16 probes were not runnable here (browser, running server,
PostgreSQL or live provider) and remain the route-level reproductions to do
first. No live provider, deployed environment, full Python suite, PostgreSQL
testcontainer contention selection or real-browser suite was run. No claim is made that every plugin or provider transport is
qualified. Refresh affected traces, controls and focused tests when `.10 lite`
changes arrive; do not carry an old defect forward solely because it appears
in this document.
