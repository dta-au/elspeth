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
  defective plugin.
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
`diff_baseline_unavailable`. Planner context and MCP pass a baseline. Controlled
handler cases distinguish missing baseline, a changed baseline and the identical
state. The attempted full-service reproduction failed at harness admission
before a provider call, so it is not end-to-end runtime evidence.

**Related evidence:** [source inspection](tools/inspect_source.md) can succeed
under a direct context that lacks the storage root and operation authority
required by `get_blob_content`. This proves tool admission inconsistency; it
does not establish a reachable unauthorized HTTP read or a concurrency exploit.

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
[`get_audit_info`](tools/get_audit_info.md) have browser tooltip claims broader
than their implementations: a model catalogue is not a deployment credential
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
lookup, whereas other blob paths canonicalize the identity. The canonical ID
succeeds and the uppercase representation of the same identity reports missing.
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
custody machinery is valuable, but a new model call does not carry a generally
advertised exactly-once replay identity. This is not evidence that completed
operation replay duplicates a blob or that durable drivers lack recovery.

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
an invalid queue is stored or executed as a collector.

**Other findings/limitations:** [SG-02, `splice_transform`](tools/splice_transform.md)
works on the valid control graph but refuses an insertion when unrelated
pre-existing missing-output findings leave the full candidate invalid. The
full-validity precondition is not adequately explained in its selection guidance.
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

**Orchestrator consequence:** the model's terminal call, advisor stop, pending
review, user handoff and committed pipeline completion must remain different
outcomes. A first-class orchestrator needs those distinctions as owned data,
not only branch-specific control flow or prose.

**Owner and direction:** extend the existing descriptor/result mechanisms to
cover sync, async and terminal roles; retain specialized execution owners and
receipts. Descriptions and UI summaries should consume or be checked against
these contracts. This is an incremental unification of authority, not a demand
for one giant handler or one universal state store.

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
failed probes and outstanding questions. No live provider, deployed environment,
full Python suite, PostgreSQL testcontainer contention selection or browser
suite was run. No claim is made that every plugin or provider transport is
qualified. Refresh affected traces, controls and focused tests when `.10 lite`
changes arrive; do not carry an old defect forward solely because it appears
in this document.
