# `restore_parked_graph` — restore an exact parked draft

Baseline: `3b28d537be98234da8135c418bf185a998c18b68`, restored 0.8.9.

## 1. Purpose and usage

Use this terminal provider control to resume an existing parked draft by its
server-advertised identity, then communicate with the user. It does not search
by title or approximate a past graph. It is exposed on every authoring and
reply surface: the freeform loop (ordinary, last-chance and staged-review
reply calls), the empty-graph proposal planner and its escape hatch, and the
malformed-head recovery call, in both tool dialects (§3.1). Advisor
checkpoint, interpretation-review and auto-title provider calls do not carry it. It has no
synchronous `ToolDeclaration`, no Composer MCP route, no REST route and no
browser tool card. A live fenced COMPOSE operation, provider authority, a
session-scoped park identity and provider budget are required. Restoration can
be refused even though the model requested it.

The only place the model learns a park identity is the per-turn recovery
context (`parked_graphs`: `park_id`, `state_id`, `version`, newest 100 only)
and earlier `Draft parked as <park_id>` receipts in chat history. No tool can
preview a park's content before restoring it. `diff_pipeline` takes no
arguments and compares only with the surface-supplied baseline
([declaration](../../../../src/elspeth/web/composer/tools/generation.py#L4680)).

## 2. Contract

Both fields are required: `park_id` is a nonblank string of 1–128 characters;
`message` is a nonblank string of 1–16,000 characters. Extra fields are rejected,
and credential material in the message is refused. No field has a default.
The control must be alone in its response. The owned intent adds the bounded
provider call identity and fixes action to `restore_parked_graph`.

`park_id` is not canonicalized at admission: an in-memory parser run accepted
uppercase, brace-wrapped, whitespace-padded and non-UUID strings unchanged
(restore_parked_graph-N3). The store then compares that raw string with the
canonical lowercase identity it advertised. A probe run (§7.1) confirmed that
uppercase, braced and padded spellings of an advertised park are refused as
`target_unavailable`.

Successful parsing creates an intent only. Session preparation looks up the
park by both park ID and current session, reads and qualifies its exact graph,
and prepares a new state identity with recovery provenance. Final operation
completion performs the write, preserves required evidence, and returns the
actual state plus trusted recovery notice. Unknown, foreign, non-canonically
spelled, unreadable, unrepresentable or credential-bearing targets all become
one `target_unavailable` refusal (restore_parked_graph-N4); the prospective
model success message is withheld in favour of trusted failure wording.

A successful restore writes more than graph state. It always inserts a new
state version (even when the park points at the current head,
restore_parked_graph-N8) with `is_valid=False` and no validation errors. It
rejects every pending `set_pipeline` proposal with reason `superseded`
(restore_parked_graph-N1), but it leaves other pending proposals pending with a
stale base (restore_parked_graph-N2). It carries over only a *negative*
(blocking) advisor review fact from the target. A positive review, damaged
legacy gates and validation results do not transfer.

This has no discovery cache, no automatic handler retry, and no independent
proposal approval. The enclosing provider/operation lifecycle owns time,
quota, cancellation and replay. A completed operation replays without writes;
a new operation can restore the same snapshot again and create new provenance.
The park row is not consumed. The COMPOSE fence and head comparison reject stale
or foreign work. It restores graph state only: no external-effect reversal,
resource resurrection, credential grant, execution approval, or unconditional
validation success is implied.

## 3. Implementation trace

- [`RestoreArguments`](../../../../src/elspeth/web/composer/recovery_controls.py#L29),
  [definitions and description](../../../../src/elspeth/web/composer/recovery_controls.py#L35)
  (length keywords stripped at
  [L55–61](../../../../src/elspeth/web/composer/recovery_controls.py#L55)),
  [strict local parsing](../../../../src/elspeth/web/composer/recovery_controls.py#L69),
  [mixed-batch rejection](../../../../src/elspeth/web/composer/recovery_controls.py#L86),
  [scalar context projection](../../../../src/elspeth/web/composer/recovery_controls.py#L99).
- [Owned intent bounds](../../../../src/elspeth/web/composer/graph_recovery.py#L41),
  [park bookmark shape](../../../../src/elspeth/web/composer/graph_recovery.py#L61),
  [editor qualification](../../../../src/elspeth/web/composer/graph_recovery.py#L190).
- [Freeform provider list](../../../../src/elspeth/web/composer/provider_gateway.py#L391)
  and [planner list](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647).
  Strict wire schemas omit string length keywords; local Pydantic admission
  retains them.
- [Malformed-head provider route](../../../../src/elspeth/web/composer/service.py#L1028)
  supplies scalar identities, not damaged graph content;
  [planner handoff](../../../../src/elspeth/web/composer/pipeline_planner.py#L4351)
  terminates before candidate construction.
- [Turn admission loads context every turn](../../../../src/elspeth/web/sessions/composer_turn.py#L902),
  [park advertisement (newest 100)](../../../../src/elspeth/web/sessions/service.py#L6641),
  [session-scoped lookup and qualification](../../../../src/elspeth/web/sessions/service.py#L6777).
- [Actual terminal settlement and refusal handling](../../../../src/elspeth/web/sessions/composer_turn.py#L1051),
  [terminal write and proposal supersession](../../../../src/elspeth/web/sessions/service.py#L9088),
  [receipt and audit row](../../../../src/elspeth/web/sessions/service.py#L9189)
  and [browser receipt admission](../../../../src/elspeth/web/frontend/src/api/composerOperationDecoder.ts#L151).

### 3.1 Route inventory

Instruments: `grep -rn "restore_parked_graph\|RECOVERY_TOOL_NAMES\|recovery_tool_definitions\|parse_recovery_tool\|recovery_context_message" src/`.
Absence greps were run alongside a known positive: `tools/` plus `tool_batch.py`
had 0 hits (`set_pipeline` had 29 in `tool_batch.py`). `composer_mcp/` had 0
hits (`set_pipeline` had 2 in `server.py`). The frontend had 0 hits for
`restore_parked_graph|parked|park_id` (`list_models` hits `toolCallDescriptions.ts`).
`composition_graph_parks` is referenced only in `sessions/models.py` and
`sessions/service.py`.

| Route | Advertises / consumes | Gate |
|---|---|---|
| Freeform main loop ([L2591](../../../../src/elspeth/web/composer/service.py#L2591)) | Full loop list, with recovery inserted before the trailing cache anchor ([L411](../../../../src/elspeth/web/composer/provider_gateway.py#L411)); context sent as a system message ([L2464](../../../../src/elspeth/web/composer/service.py#L2464)) | Non-empty, decodable head (`planner_eligible` false, [L1234](../../../../src/elspeth/web/composer/service.py#L1234)) |
| Freeform last-chance call ([L2202](../../../../src/elspeth/web/composer/service.py#L2202)) | Same list after the composition-turn cap | `composer_max_composition_turns` reached |
| Staged-review reply stage ([L1991](../../../../src/elspeth/web/composer/service.py#L1991), [L2018](../../../../src/elspeth/web/composer/service.py#L2018)) | Recovery controls only; the gateway rejects any other call or empty text ([L826–834](../../../../src/elspeth/web/composer/provider_gateway.py#L826)) | Review cards staged, or runtime result valid |
| Planner candidate/discovery turns ([L3455](../../../../src/elspeth/web/composer/pipeline_planner.py#L3455), [L5125](../../../../src/elspeth/web/composer/pipeline_planner.py#L5125)) | Discovery + recovery + `emit_pipeline_proposal`; context as `graph_recovery_context` in the request ([L3483](../../../../src/elspeth/web/composer/pipeline_planner.py#L3483)) | Structurally empty graph with session and user-message IDs; planner discovery policy may narrow discovery only |
| Planner escape hatch ([L4268](../../../../src/elspeth/web/composer/pipeline_planner.py#L4268)) | Same list in the hatch dialect, to the advisor model | Hatch configured and engaged after a planner failure |
| Malformed-head recovery ([L1028](../../../../src/elspeth/web/composer/service.py#L1028)) | Recovery controls only; context as a user message | `recovery_context.malformed_head` ([composer_turn L1474](../../../../src/elspeth/web/sessions/composer_turn.py#L1474)) |
| Boot probe ([L215](../../../../src/elspeth/web/composer/boot_probe.py#L215)) | Advertisement only, in a hatch probe of recovery + terminal | Startup with a non-`none` hatch dialect; nothing is consumed |
| Planner → settlement ([planning_application L1181](../../../../src/elspeth/web/composer/planning_application.py#L1181)) | `PlannerHandoff` → `ComposerResult.graph_recovery_intent` | Any planner turn |
| Session settlement ([composer_turn L1051](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)) | `prepare_graph_recovery` → `complete_composer_async_operation(recovery=…)` | Live COMPOSE fence; unchanged expected head |
| Browser | `recovery_notice` decoding ([decoder L150–176](../../../../src/elspeth/web/frontend/src/api/composerOperationDecoder.ts#L150)); store adoption ([sessionStore L512–534](../../../../src/elspeth/web/frontend/src/stores/sessionStore.ts#L512)) | Operation result; no tool card, no park list |
| Next-turn model | The system-role receipt row is not excluded by the fetch ([`get_messages(…, include_private_evidence=False)`](../../../../src/elspeth/web/sessions/composer_turn.py#L979)) or by the [history builder](../../../../src/elspeth/web/sessions/routes/_helpers.py#L1839); the structured `audit` row is excluded. Provider acceptance of a mid-history system message was not checked | Chat history retained |

Not routes: the synchronous registry/dispatcher, `tool_batch.py` proposal
interception and replay, Composer MCP, REST, and async advisor/interpretation
paths. Recovery is parsed before dispatch, so it never becomes a proposal. No
tutorial-specific branch was found. `grep -i tutorial` over
`recovery_controls.py`, both `graph_recovery.py` files and the recovery
regions of `composer/service.py` (L1028–1110), `sessions/service.py`
(L6600–6860, L9088–9265) and `composer_turn.py` (L1040–1120) returned 0. The
same word matches other `composer/*.py` files.

## 4. Alternative paths

A valid same-session park restores the exact editable snapshot. The current
head may be malformed without preventing the scalar recovery route. A park
from another session, unknown park, non-canonically spelled ID, malformed
target or credential-bearing checkpoint is unavailable; nothing is
reconstructed heuristically. Mixed batches reject before discovery or
mutation effects, but the rejection ends the turn and the model gets no
repair turn. The planner settles as HTTP 502 `invalid_provider_response` /
`MALFORMED_RESPONSE`. Freeform and malformed-head raise a bare `ValueError`
that settles as HTTP 500 `operation_failed`. On the malformed-head route, a
partner tool outside its recovery-only palette is refused first by the
gateway, as HTTP 502 `composer_error` (restore_parked_graph-N5, measured in
§7.1). The single provider call is still drained and persisted
([finally block](../../../../src/elspeth/web/sessions/composer_turn.py#L1876)).
Credential material in `message` raises `CredentialMaterialRefused`, which is
an admission refusal and not a `ValueError`, on every route. Provider
failure, budget expiry, stale base, fence loss, a newer head from another
operation, and failed persistence do not become success. Completed replay
returns the original durable terminal response even after fence release.
Cancellation tests cover settlement evidence independently of the normal
success response.

## 5. Usability assessment

**Intended restriction, high confidence:** no execution readiness is granted.
Missing files, resources, reviews or secrets can remain after a valid restore;
inspect/preview the result and obtain required review before execution.

**Source-established discoverability limitation, high confidence:** context
advertises at most 100 newest parks without a cursor or truncation flag. The
store lookup itself accepts an older known same-session identity, so do not
claim data loss or universal inability to restore. Model-directed selection of
an unadvertised older park lacks a first-class discovery route. No >100-park
interaction was executed during this assessment.

**restore_parked_graph-N1, demonstrated (probe run at the session-service layer, §7.1).** A
successful restore rejects every pending `set_pipeline` proposal as
`superseded`
([L9117–9166](../../../../src/elspeth/web/sessions/service.py#L9117)). This
happens even when the restored graph equals the head. In the probe run, the
pending pipeline proposal moved to `rejected` with a `proposal.rejected` event
by `composer_graph_recovery`, for a park of an older state and for a park of
the current head. A refused restore left it `pending`. The declaration and
skills do not mention this, and neither does the receipt notice. The measured
notice was "Restored saved draft … as a new editable version. Execution
readiness and approvals require current checks. …", with no word about
proposals. The repository suite still tests only the refusal path
(`test_completed_restore_refusal_preserves_head_and_pending_proposal`). A grep
for `superseded`, `proposal.rejected` and `composer_graph_recovery` found no
success-path assertion in the six test files that call
`prepare_graph_recovery` or cover turn recovery. The actor string is present in
`service.py`, which confirms the grep can match.

**restore_parked_graph-N2, demonstrated inconsistency (session layer and full turn path, §7.1).** Supersession
filters `tool_name == "set_pipeline"`
([L9124](../../../../src/elspeth/web/sessions/service.py#L9124)), so pending
incremental proposals stay pending after a restore. In the probe run, a
pending `set_metadata` proposal stayed `pending` while the `set_pipeline`
proposal beside it was rejected. Through `POST /messages`, the completed
restore response listed it under `proposals` as `pending`. Accepting it then
returned 409 `proposal_base_state_changed`
([proposals L409–418](../../../../src/elspeth/web/sessions/routes/composer/proposals.py#L409)).
The control accepted the same proposal without a restore turn and got 200
`committed`. The base check exempts proposals whose blob effect was applied
(`blob_effect_applied`). That branch was neither traced nor run. No unsafe
approval was shown. The defect is a dead card with inconsistent disposition.

**restore_parked_graph-N3, demonstrated on SQLite (parser to store, and full turn path, §7.1).**
`parse_recovery_tool_call` accepted `UPPER`, `{braced}`, `" padded "` and
`park-one` IDs unchanged. Lookup is raw string equality on a `String` column
([L6777–6784](../../../../src/elspeth/web/sessions/service.py#L6777)), while
advertised IDs are canonical lowercase
([`recovery_identity`](../../../../src/elspeth/web/sessions/graph_recovery.py#L33)).
In the probe run, the admission context advertised the canonical lowercase ID.
The parser passed each spelling through unchanged. The uppercase, braced and
padded spellings each settled as `target_unavailable`, the canonical spelling
restored the parked state, and an unknown UUID was also refused. A scripted
model call with the uppercase ID through `POST /messages` completed as a
refusal ("Graph recovery did not complete …"), and the head stayed unchanged.
The canonical ID on the same path restored. The PostgreSQL leg was not run,
because Docker is unavailable to this probe. The column is `String` on both
backends, and default collations compare it case-sensitively on both. That
part is reasoning, not a run. This is the same class as
BOS-03.

**restore_parked_graph-N4, source-established actionability gap.** All refusal
causes share `failure_reason="target_unavailable"`
([L6737](../../../../src/elspeth/web/sessions/service.py#L6737)) and one notice
([L9203](../../../../src/elspeth/web/sessions/service.py#L9203);
[assistant notice](../../../../src/elspeth/web/composer/no_tool_policy.py#L96)).
That notice tells the user to "Choose another saved draft", but the user has no
park list in the browser or REST surface. Not disclosing *content* is
intended. Collapsing "no such park in this session" and "park exists but
cannot be restored" is not needed for that and leaves the next turn guessing.

**restore_parked_graph-N5, demonstrated route divergence (full `POST /messages` worker path, §7.1).**
A malformed restore control gets different user-visible failures by route.
The planner converts it to `MALFORMED_RESPONSE` with an audited
planner-failure body
([L4361](../../../../src/elspeth/web/composer/pipeline_planner.py#L4361)).
Freeform and malformed-head re-raise the `ValueError`
([L1094–1099](../../../../src/elspeth/web/composer/service.py#L1094),
[L1078–1084](../../../../src/elspeth/web/composer/service.py#L1078)). No typed
turn arm catches it, so the worker settles it as a generic failure. These were
the measured settlements:

| Route | 129-character `park_id` | Mixed batch |
|---|---|---|
| Freeform | HTTP 500 `operation_failed` (Pydantic `string_too_long`) | HTTP 500 `operation_failed` (`[restore, set_metadata]`, "sole tool call") |
| Malformed head | HTTP 500 `operation_failed` | `[restore, set_metadata]`: HTTP 502 `composer_error`, "Reply stage requires text or a terminal recovery control" (the gateway's recovery-only palette refuses it before the parser). `[restore, handoff_to_user]`: HTTP 500 `operation_failed` |
| Planner | HTTP 502 `invalid_provider_response` / `MALFORMED_RESPONSE` | HTTP 502 `invalid_provider_response` / `MALFORMED_RESPONSE` |

The 500 bodies carry only "The compose operation failed" and a diagnostic ID.
Every run made exactly one provider call and persisted exactly one
`llm_call_audit` row, so the drain claim holds. No route gave the model a
repair round, although each double had more responses queued. The freeform
head was unchanged. The control was a well-formed restore of an unknown UUID.
On all three routes it completed as a trusted refusal ("The requested graph
recovery did not complete"), so the failures above come from the malformed
control and not from the refusal. Ordinary tool-argument rejection, by
contrast, gives the model a repair opportunity.

**restore_parked_graph-N6, source-established discovery limitation.** Each
advertised park carries only `park_id`, `state_id` and `version`
([L108–110](../../../../src/elspeth/web/composer/recovery_controls.py#L108)).
The stored `created_at` is used for ordering but not advertised, and there is
no label or eligibility flag. Parks are not qualified when they are
advertised, so an advertised park can still be refused at settlement. The
scalar-only design is intentional: it isolates a malformed head and keeps
untrusted graph text out of context. Server-owned scalars such as
`created_at` would not break that.

**restore_parked_graph-N7, intended restriction with a guidance consequence.**
The control is advertised on every route even when `parked_graphs` is empty.
An in-memory run of the context projection for a session with no parks gave
`"parked_graphs": []`, and both producers still listed the tool. A stable
palette protects the cache anchor and strict-dialect tests. The consequence is
that any call in that state is a guaranteed refusal that ends the turn.

**restore_parked_graph-N8, demonstrated no-op semantics (SG-03 class, §7.1).**
Every restore uses a fresh `state_id`
([L6849–6851](../../../../src/elspeth/web/sessions/service.py#L6849)), so a park
of the current head still creates a version and supersedes `set_pipeline`
proposals. In the probe run, restoring a park of head v2 wrote a new v3 with
identical content, `is_valid=False`, derived from v2. It also rejected the
pending `set_pipeline` proposal. The full-turn run gave the same result: a
park of the seeded head produced v2 and left the incremental proposal stale
(N2).

**restore_parked_graph-N9, source-established audit labelling, low.** The
planner attempt trail records a restore as phase `prose`, outcome `declined`,
code `DECLINED`
([L4363](../../../../src/elspeth/web/composer/pipeline_planner.py#L4363);
[`PlannerHandoff`](../../../../src/elspeth/web/composer/pipeline_planner.py#L700)).
The session `graph_recovery` audit row records the real action, so nothing is
lost.

**restore_parked_graph-N10, source-established audit shape, low, mitigated.**
For a restore, the `graph_recovery` audit row's `park_id` field is null,
because the field names a *created* park. On refusal `target_state_id` is also
null
([L9238–9253](../../../../src/elspeth/web/sessions/service.py#L9238)). The
requested ID is kept in private diagnostic evidence (asserted by
`test_refused_provider_recovery_retains_exact_private_control_and_physical_binding`),
not in the public audit row.

No demonstrated integrity defect was found in the inspected local recovery
paths. The current tests provide useful counterevidence to a hypothesis that a
model can force an arbitrary restore or publish success for a missing target.

### 5.1 Guidance parity

Instrument: `grep -n -i "restore_parked_graph\|handoff_to_user\|park\|recovery\|restore\|checkpoint\|saved draft"`
over both skills. The control `set_pipeline` matched 31 lines in
`pipeline_composer.md` and 2 in `pipeline_capabilities.md`.

- **Accurate:** [`pipeline_composer.md` L66–96](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L66)
  (use alone, ends the turn, explicit IDs only, no silent substitution, no
  readiness or effect reversal, phrase the message as the requested action)
  and the termination state at [L1359–1360](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1359)
  match source. The declaration description matches as well.
- **Missing argument guidance:** the emitted schema gives each property a
  `title` and no `description`. Nothing in the declaration names
  `parked_graphs[].park_id` as the source. The skill says "session context",
  while the payload is an unlabeled system JSON message (freeform),
  `graph_recovery_context` (planner) or a user message (malformed head).
- **Undisclosed effects:** neither the description nor the skill says that
  restore supersedes pending `set_pipeline` proposals (N1), always creates a
  new version (N8), or turns an unknown ID into a turn-ending refusal with no
  reason (N4, N7).
- **Malformed-head prompt bias:** [service.py L1063–1068](../../../../src/elspeth/web/composer/service.py#L1063)
  explains only `reset_to_safe_checkpoint` and does not mention that parked
  graphs are listed or that `restore_parked_graph` exists. The tool
  description alone carries it.
- **Receipt wording overstated:** the skill says the receipt reports "whether
  the displaced draft was retained". For restore and reset,
  `displaced_candidate_captured` is hard-coded `False`
  ([L9250](../../../../src/elspeth/web/sessions/service.py#L9250)), which is
  honest but never varies.
- **User notice dead end:** "Choose another saved draft" (N4) has no
  user-facing affordance.
- **`pipeline_capabilities.md`** has no recovery-control guidance. Its matches
  are runtime checkpoint terms. That skill covers plugin capability, so no
  addition is needed there.
- Planner text ([L1936–1944](../../../../src/elspeth/web/composer/pipeline_planner.py#L1936),
  [hatch L1960](../../../../src/elspeth/web/composer/pipeline_planner.py#L1960))
  and the reply-stage prompt ([L1979](../../../../src/elspeth/web/composer/service.py#L1979))
  correctly say restore is available and must be used alone. No contradiction
  between the two skills was found.

## 6. Structural remediation

The orchestrator should receive an owned terminal outcome with requested action,
actual settlement/refusal, restored state identity and remaining readiness work.
A unified tool descriptor should carry async/terminal lifecycle and authority
requirements while leaving the atomic write with Sessions. This also affects
`handoff_to_user`, planner proposal completion and advisor termination. The
outcome should also list the proposal dispositions the restore caused (N1/N2).
Malformed controls should map to one owned refusal category on every route (N5).

Give park discovery explicit paging/completeness and same-session identities.
Preserve existing park IDs and stored state history; a new response envelope
must be versioned or adapted without weakening preserve/refuse behavior.
Decide one pending-proposal policy for head-changing recovery: supersede all
kinds, or supersede none and let base checks refuse. The receipt should report
that choice.
Implementation risk is high around stale-head and receipt semantics; a descriptor
change alone is reversible. Acceptance: known same-session exact restore;
unknown/foreign/corrupt target refusal; empty/oversize/extra argument rejection;
mixed batch produces no effects; changed head/fence loss rejects; cancelled
audit settles once; completed replay writes nothing; >100 parks are all
discoverable or explicitly marked incomplete; equivalent ID spellings resolve
identically or refuse uniformly; restore with pending `set_pipeline` and
incremental proposals leaves each in its declared disposition.

### 6.1 Simple enhancements

1. **Argument descriptions.** Add `Field(description=…)` to `park_id` ("a
   `park_id` from `parked_graphs` in the graph recovery context; never
   invented") and `message` at
   [recovery_controls.py L29–32](../../../../src/elspeth/web/composer/recovery_controls.py#L29).
   This closes the gap where the declaration never names the ID source.
   Acceptance: both dialects emit the descriptions, and the strict palette and
   boot-probe parity tests stay green.
2. **Disclose effects in the description** at
   [L50–51](../../../../src/elspeth/web/composer/recovery_controls.py#L50):
   pending pipeline proposals are superseded, a new version is created, and an
   unknown ID ends the turn with a refusal. This closes N1, N7 and N8 for the
   model. Acceptance: a description test pins the text.
3. **Canonicalize `park_id` at admission.** In
   [L77–82](../../../../src/elspeth/web/composer/recovery_controls.py#L77),
   replace a UUID-parseable value with `str(UUID(value.strip()))` and leave
   other strings raw so they refuse at lookup. This closes N3. Acceptance:
   uppercase, braced and padded spellings of an advertised park restore that
   park, and a non-UUID value is still `target_unavailable`.
4. **Split refusal reasons** at
   [L6784](../../../../src/elspeth/web/sessions/service.py#L6784) and
   [L6797–6800](../../../../src/elspeth/web/sessions/service.py#L6797) into
   "not found in this session" (unknown and foreign together, so non-disclosure
   holds) and "found but not restorable". Carry the reason into the receipt
   text at [L9203](../../../../src/elspeth/web/sessions/service.py#L9203).
   This makes N4 actionable. Acceptance: the foreign and unknown cases stay
   byte-identical, and the unrestorable case is distinct. The audit vocabulary
   is persisted, so treat this as an additive value.
5. **Advertise server-owned park scalars.** Add `created_at` and a
   `parked_graphs_truncated` flag (query `limit(101)`) at
   [graph_recovery.py L61–67](../../../../src/elspeth/web/composer/graph_recovery.py#L61),
   [recovery_controls.py L108–110](../../../../src/elspeth/web/composer/recovery_controls.py#L108)
   and [service.py L6642–6659](../../../../src/elspeth/web/sessions/service.py#L6642).
   This addresses N6 and the 100-park window. Acceptance: 100 parks give
   `false`, 101 parks give `true`, and no graph content enters context.
6. **Name restore in the malformed-head prompt** at
   [service.py L1063–1068](../../../../src/elspeth/web/composer/service.py#L1063):
   say that parked graphs are listed in the next message and can be restored
   by ID. Acceptance: a prompt test asserts all three actions are named.
7. **Fix the dead-end user notice** at
   [no_tool_policy.py L96–98](../../../../src/elspeth/web/composer/no_tool_policy.py#L96):
   ask the user to describe which saved draft they want, or how to continue.
   Acceptance: a notice text test.
8. **Classify freeform malformed controls.** Wrap the `ValueError` from
   [L1094–1099](../../../../src/elspeth/web/composer/service.py#L1094) and
   [L1078–1084](../../../../src/elspeth/web/composer/service.py#L1078) in a
   typed composer error with a stable code that matches the planner's
   `MALFORMED_RESPONSE`. Acceptance: a turn-level mixed batch and a
   129-character ID each settle with that code, provider calls persist once,
   and no effects occur.
9. **Missing tests:** success-path `set_pipeline` supersession (proposal row
   `rejected`, one `proposal.rejected` event by `composer_graph_recovery`); an
   incremental proposal left pending then 409 on accept (N2); restore of the
   current head's park (N8); a planner trail row label for restore (N9).

## 7. Evidence, confidence, risk and limits

Inspected and ran
[planner recovery](../../../../tests/unit/web/composer/test_planner_graph_recovery.py),
[session turn recovery](../../../../tests/unit/web/sessions/test_graph_recovery_turn.py),
and [adversarial recovery](../../../../tests/unit/web/sessions/test_graph_recovery_adversarial.py)
within the parent's six-file selection: **86 passed, exit 0**. Meaningful
assertions include exact park identity, pre-dispatch mixed-batch rejection,
trusted missing-target refusal, malformed-head isolation, cancellation evidence
and completed replay with unchanged stored rows. These are scripted/local and
SQLite observations. PostgreSQL contention, live transports, large park history
and browser rendering were not exercised. Confidence is high for local/source
contracts; deployment portability needs its own required testcontainer gate
when implementation touches persistence or coordination.

Additional bodies read but not run for the sheet review:
[persistence round trip](../../../../tests/unit/web/coordination/test_graph_recovery_persistence.py#L340),
[security cases](../../../../tests/unit/web/sessions/test_composer_recovery_security.py#L77)
(foreign or unknown park, normalization refusal, refusal that preserves a
proposal),
[recovery palettes](../../../../tests/unit/web/composer/test_recovery_control_palettes.py#L245)
(mixed batch raises `ValueError`; reply-stage and pending-review recovery), and
[negative-only review retention](../../../../tests/unit/web/sessions/test_graph_review_facts.py#L737)
(exercised with `reset_to_turn_start` on the shared settlement path). The
sheet review itself ran no pytest. Its N7 context observation comes from
in-memory calls to `recovery_context_message` and the two tool-list
producers, with `elspeth.__file__` resolving into this checkout, and is not an
end-to-end run. The later probe runs in §7.1 cover N1, N2, N3, N5 and N8.
Nothing has been implemented.

### 7.1 Probe runs (2026-11-09)

Worktree `$WT` (production source at baseline `3b28d537b`), lane-private
directory `$L`. `$PY` is the main checkout's `.venv/bin/python`. Every run used
the network fail-closed prefix `HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY= LITELLM_LOCAL_MODEL_COST_MAP=True ELSPETH_DISABLE_NETWORK=1`,
plus `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src`,
`TMPDIR=$L/tmp` and `-n 0 -p no:cacheprovider --rootdir $WT -c $WT/pyproject.toml`
with a matching whitespace-separated `-o pythonpath`. Ad-hoc files loaded
`-p tests.conftest -p tests.unit.conftest`. The session-layer file also loaded
`-p tests.unit.web.coordination.test_composer_operation_authority` for
`operation_store`. Each file asserted two things: that `elspeth.__file__`
resolves under `$WT/src/`, and that the autouse fixtures loaded. The shell
unset `ELSPETH_ALLOW_RAW_SECRETS`, and the test saw `"true"`.
A proxy control (`$L/proxy_control.py`) returned `ConnectError [Errno 111]`.
The provider guard replaced `litellm.acompletion` with a scripted double that
raises on any unscripted call, and made `litellm.completion` raise.
`provider_gateway._litellm_acompletion` and the by-name
`pipeline_planner._litellm_acompletion` raise unless that double is installed.
Its positive control showed every unscripted entry point raising.

| Probe | Finding | Outcome | Command (abridged) | Exit | Observed |
|---|---|---|---|---|---|
| P0 | baseline | ran, green | `$PY -m pytest tests/unit/web/sessions/test_graph_recovery_turn.py` | 0 | 5 passed |
| P1 | N5 | **confirmed** | `$PY -m pytest $L/test_probe_n5.py …` | 0 (12 passed) | `POST /messages` through the detached worker with the real `ComposerServiceImpl`. Controls: a well-formed unknown-park restore completed as a trusted refusal on freeform, malformed-head and planner. Malformed controls: freeform 500 `operation_failed` for both shapes. Malformed head: 500 for a 129-character ID or a pair of recovery controls, 502 `composer_error` for `[restore, set_metadata]` (gateway palette check). Planner: 502 `invalid_provider_response`/`MALFORMED_RESPONSE` for both shapes. One provider call and one `llm_call_audit` row per run, no repair round, freeform head unchanged |
| P2 | N1, N2, N8 | **confirmed** | `$PY -m pytest $L/test_probe_session.py …` (session layer) and `$PY -m pytest $L/test_probe_route.py …` (full turn) | 0 (5 passed), 0 (5 passed) | Session layer: one pending `set_pipeline` proposal and one pending `set_metadata` proposal on head B. Restoring park→A produced v3 "Parked draft A", `is_valid=False`; the pipeline proposal became `rejected` by `composer_graph_recovery`, the incremental stayed `pending`, and the receipt did not mention proposals. Park→B (current head) gave the same dispositions and a new v3 with identical content (N8). A refused restore left both pending. Limit: the session-layer admission context was loaded when the operation started, before the states and proposals existed. That does not affect the result, because supersession and park lookup are keyed on DB rows, and the full-turn run agrees. Full turn: the restore response listed the incremental proposal as `pending`, and accept returned 409 `proposal_base_state_changed`. Control: accept without a restore turn returned 200 `committed` |
| P3 | N3 | **confirmed** (SQLite) | `test_probe_session.py::test_n3_spellings` and `test_probe_route.py::test_n3_end_to_end_spelling` | 0 | Advertised `[<canonical lowercase id>]`. The parser left every spelling unchanged. Canonical restored A. Uppercase, braced and padded spellings each gave `target_unavailable`, as did the unknown-UUID negative control. Full turn: uppercase completed as a refusal with the head unchanged, and canonical restored |
| P3-PG | N3 | not runnable here | — (no file written; the N3 cases would need a testcontainer-marked copy) | — | Needs PostgreSQL/Docker, which this probe brief forbids |

## Runtime argument schema and measured exposure

This async/terminal control has no synchronous declaration; its specialized owner is traced above.

| Surface | Exposed |
|---|---|
| Canonical emitter | no |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness. Re-measured during the sheet review: in both dialects the tool is at loop index 43 (0-based) of 45, immediately before the trailing cache anchor, and in the planner's trailing triple `handoff_to_user, restore_parked_graph, emit_pipeline_proposal`; `openai_strict` adds `"strict": true`.

This is the emitted recovery transport schema. It deliberately omits string length keywords; the semantic Pydantic parser still enforces the bounded/nonblank fields described above. Dynamic plugin options are governed by the selected plugin schema and policy projection, not by arbitrary object permissiveness here.

```json
{
  "additionalProperties": false,
  "properties": {
    "park_id": {
      "title": "Park Id",
      "type": "string"
    },
    "message": {
      "title": "Message",
      "type": "string"
    }
  },
  "required": [
    "park_id",
    "message"
  ],
  "title": "RestoreArguments",
  "type": "object"
}
```
