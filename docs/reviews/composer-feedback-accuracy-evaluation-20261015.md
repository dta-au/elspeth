# Composer feedback accuracy evaluation — 2026-10-15

**Verdict: targeted improvements needed.** The reviewed paths have substantial controls against fabricated readiness, leaking private causes, and treating tool text as policy. However, safe diagnostic facts are lost on one planner path, some advisor failures receive an unsupported response diagnosis, and retry controls can overstate impossibility or override a nonretryable disposition. No new P0/P1 defect is established. John subsequently directed the integrator to include the reviews' confirmed findings in 0.8.6 and obtain a local full 12-worker green run before the next upload. This reviewer remains feedback-only; this report supplies the finite action inventory, not release qualification.

## Scope, identity and execution boundary

Reviewed the candidate at `.claude/worktrees/release086-email-bugs-20261014`. The integrator and `CI386712-CSV-FREEZE.json` identify its HEAD as `8ab3eae594e05b704a91615cd6ec11b586e17bae`, with frozen uncommitted repairs. HEAD was **supplied**, not independently queried through Git. The freeze document's stated digest is `53447bd0eec572b4ad6f0c10f8128538f0b1fdabdb29b5f2a2e10ee814a8a7ce`.

Read `CI386712-CSV-IMPLEMENTATION.md` as an evidence index, then independently read the relevant source. Direct `sha256sum` output for the three production files matched the freeze entries:

```text
6da15114ac1e807d8c205571dd4f87f6381bb041ee3ce9aba22e93506d7b4c70  src/elspeth/web/composer/turn_effects.py
9db29afb0ba246a93d966aa20db8b536f961c6db027d011bd117ea71ec22a56e  src/elspeth/web/composer/advisor_checkpoint.py
9add48bb2edac09caf9988b569c21430c2e36446fb159c5d33f8658d18b99935  src/elspeth/web/composer/advisor_context.py
```

Additional directly read source identities:

```text
c47e652a0425f0955093a9c66a17b97fb5778942875fc97606c9f90694f08e69  src/elspeth/web/composer/pipeline_planner.py
2fb4027633a8540b5a606b4ee54608a3b41795e30d045c9682fd5ece7692071f  src/elspeth/web/composer/provider_discovery_response.py
401634535d9134b57965c408e86fdc494223d4cef42c13be28cd3429c2a5be44  src/elspeth/web/composer/provider_errors.py
cc61ed04d6c6e618ea1a55cc418cc379da7cf4ae7d3f854dc9488c57aa335882  src/elspeth/web/composer/advisor_policy.py
2dbed4656325cfb6aa0fa822539f6fd5f1c5d3d0214c359970e81f2c4e598612  src/elspeth/web/composer/composition_completion.py
2444784f70e6338771028cc0c4b845a2f2afde8aa7d958aa2bb18f5849e6c5f2  src/elspeth/web/composer/tool_error_payloads.py
5be09b346fe1a8802b1e02b4605a0468b444bd862faf9c2f95430cd64f01c882  src/elspeth/web/composer/tool_batch.py
```

All source references below are relative to that worktree and use its observed line numbers. This was source inspection only: no tests, imports, application execution, provider or network calls, mutations to production/test/source files, runner changes, commits, or release operations. The only deliverable written is this report in the main checkout. Immediately before writing, `git check-ignore -v docs/reviews/composer-feedback-accuracy-evaluation-20261015.md` returned exit `1` with no output. No live registry inventory or incident-rate claim is made. Regex searches located code; they were not used as a census or acceptance instrument.

Applied the LLM-specialist skill and its tool/context/prompt sheets, plus the SME qualification protocol. The actual `AGENTS.md` Composer invariants remain constraints: corrections must preserve provider authorship and the shared tutorial backend. Historical diagnostic/recovery memory informed scope and evidence boundaries only; findings below come from current source.

## Ranked findings

### F1 — [P2] A coarse repeated-rejection identity is treated as proof that repair cannot converge

**Confidence: Moderate for the complete scenario; High for the control and wording. [Static; consequence derived; not reproduced.]**

`pipeline_planner.py:2249-2268` fingerprints only sorted `(component, error_code)` pairs. It does not bind the conflicting field, types, candidate, or private diagnostic facts. `pipeline_planner.py:2411-2423` marks row-union/coalesce type facts withheld whenever **any** finalizer configuration ownership exists, including ownership unrelated to the failing branches. On a repeated fingerprint plus any withholding, `pipeline_planner.py:4619-4660` immediately enters the hatch/terminal path and records that the rejection “cannot converge.” The model-facing notice at `pipeline_planner.py:2282-2287` says “Only a structurally different candidate — or an honest decline — can resolve this.”

**Concrete trigger → cause → feedback → corrected meaning:** A model-authored union coalesce has two shared-field type conflicts, `x` and `y`, while a required-control finalizer introduces an unrelated config-owned component. The first validation reports the conflict in `x`; the model aligns `x` but leaves `y`. The coalesce producer reports one conflict and then continues at `state.py:5748-5776`, so the next rejection can have the same coalesce component and code but a different remaining field. The broad withholding condition suppresses those facts. The repeated fingerprint then yields the impossibility notice and short-circuit, although one substantive repair succeeded and another schema repair could finish the same topology. The truthful meaning is: **the visible component/code set repeated, and some evidence is withheld; that alone establishes neither identical causes nor impossibility.**

**Counterevidence and limits:** Ordinary fully disclosed repeats do not take this short-circuit. Finalizer ownership is derived from the actual before/after candidate (`pipeline_planner.py:1043-1080`), and withholding cross-component facts protects private configuration. The coalesce fact projection and its intentional suppression are explicit (`pipeline_planner.py:2674-2682`; existing test source `test_pipeline_planner.py:2856-2904`). None of those controls makes the coarse fingerprint a progress measure. The two-conflict scenario requires a future deterministic end-to-end fixture; it is not a reported production incident.

**Minimal guarded option:** Keep withholding. Describe repeated visible classifiers without claiming impossibility. Gate the early stop on an internal semantic rejection/candidate identity that distinguishes repaired facts, or retain the existing bounded repair budget when progress cannot be proved absent. A private fingerprint must remain internal and must not become a provider-visible digest oracle. This is materially relevant to convergence under required controls, but no attribution to the frozen CSV repair or release introduction is established.

### F2 — [P2] An advisor API/request failure can become “advisor response was malformed” without response evidence

**Confidence: High. [Static; not executed.]**

The shared classifier identifies a LiteLLM bad request as `BAD_REQUEST_ERROR`, kind `bad_request`, nonretryable (`provider_errors.py:35-38`), and generic API errors as an unavailable/API family (`provider_errors.py:41-47`). The advisor call records that classification and re-raises (`advisor_checkpoint.py:477-482`). The checkpoint catches admitted provider failures (`advisor_checkpoint.py:1245-1259`), then uses a narrower unavailable-type list; other API/bad-request failures fall through to `malformed` (`advisor_checkpoint.py:1312-1338`). `advisor_policy.py:59-64` renders that as the literal **“advisor response was malformed.”**

**Concrete trigger → cause → feedback → corrected meaning:** The advisor dispatch raises a LiteLLM `BadRequestError` before any usable advisor content is returned, for example because the provider rejects the request. The audit knows `BAD_REQUEST_ERROR`; the final advisory result instead says the advisor response was malformed. The terminal builder stores `AdvisorBlockCause.MALFORMED` and uses that wording (`advisor_checkpoint.py:837-859`), with the recovery suggestion “The advisor returned no usable verdict; check the advisor model configuration” (`advisor_policy.py:500-503`). A provider rejection does not establish that the model emitted malformed content. The truthful public statement is: **the completion review could not obtain a usable verdict because the provider request failed/rejected; the private cause is withheld.**

**Counterevidence and limits:** Real unparseable replies are correctly distinguished from outages at `advisor_checkpoint.py:1270-1294`; concrete timeout/auth/connection/service-unavailable failures have their own unavailable route. Controlled-code failures are intentionally excluded from the recoverable provider family (`provider_gateway.py:78-106`). The conservative catch policy is deliberate, but conservative disclosure should reduce specificity rather than invent response provenance. This finding primarily affects terminal/durable and user-visible feedback; the same-turn failed advisor is not injected into the primary model as a graph repair instruction. Later validation/readiness explanations can consume the durable block.

**Minimal guarded option:** Use neutral fixed wording for ambiguous provider failures (“no usable completion-review verdict was obtained”), reserving malformed-response wording for an observed malformed-response path. A future narrow owned failure disposition can preserve provider rejection separately if operationally useful. Do not expose raw SDK text, request bodies, or credentials. No admission or readiness relaxation is needed.

### F3 — [P2] The outer completion-review loop retries an inner nonretryable provider disposition

**Confidence: High for source control flow; operational frequency unmeasured. [Static; not executed.]**

`advisor_checkpoint.py:1254-1258` explicitly breaks its local retry loop when `classify_provider_failure` returns `retryable=False`, explaining that repeating a rejected request cannot repair it. That retry disposition is absent from the returned `AdvisorCheckpointVerdict`; its result is only an unsuccessful failure class (`advisor_checkpoint.py:1340-1346`). The outer terminal gate at `composition_completion.py:2085-2119` calls the checkpoint again until its pass budget is spent, stopping for `budget_exhausted` or `verdict.ok`, not for a nonretryable provider failure.

**Concrete trigger → cause → feedback → corrected meaning:** An advisor authentication or bad-request error stops the inner retry, but returns `ok=False`. With outer passes and deadline remaining, the same unchanged provider configuration is called again. The primary/user ultimately sees review unavailable/malformed rather than a distinction between a transient re-ask and an operator/configuration intervention. “Do not retry this disposition within the unchanged request” is lost at the owner boundary.

**Counterevidence and limits:** Calls remain bounded by pass, cost, attempt, and deadline controls; this is not an unbounded loop. Transient outage and formatting recovery legitimately benefit from an outer re-ask. The primary provider loop honors the retry classifier directly (`pipeline_planner.py:3780-3803`); the advisor's local loop does too. The defect is composition of those otherwise sensible controls. No paid-cost total, incident rate, or default execution count was measured here.

**Minimal guarded option:** Carry a closed retry disposition out of the checkpoint and stop the outer automatic re-review for a nonretryable failure. Preserve retry for transient failures and observed format errors, and preserve a later user message's ability to try after external configuration changes. Do not solve this by globally disabling advisor re-review or treating a failed review as clearance.

### F4 — [P2] Planner argument feedback discards safe, already-owned localization facts

**Confidence: High for information loss; Moderate for its effect on model convergence. [Static; not executed.]**

The tool schema boundary constructs a closed `ToolArgumentError` with schema violations, location tokens and a category (`tools/_dispatch.py:583-622`). For normal Composer dispatch, `tool_error_payloads.py:48-102` emits canonical validation errors and targeted recovery instructions, and `tool_batch.py:2842-2856` sends them to the model. In planner discovery, the same exception becomes only component, severity, generic code and class (`provider_discovery_response.py:39-56`; `pipeline_planner.py:4872-4900`). Serialization at `pipeline_planner.py:2977-2983` does not restore the safe violations. Only a rejected wire wrapper receives an extra repair message.

**Concrete trigger → cause → feedback → corrected meaning:** The planner calls `get_plugin_schema` with `plugin_type='node'`. Its declared enum is `source|transform|sink` (`tools/generation.py:339-353`); the S gate therefore constructs a closed `invalid_choice` violation at `plugin_type`. The planner receives `data.argument_error` naming `get_plugin_schema arguments`, `SCHEMA_VALIDATION`, and `ToolArgumentError`, but no failed field or choice rule. The actual actionable fact is safe: **`plugin_type` has an invalid choice; use its advertised enum.** The published schema permits a diligent model to infer the repair, but the diagnostic itself loses a fact its producer already knows. Other simultaneous type/bound failures become similarly indistinguishable.

The terminal structural precheck has a related localization gap: `_structural_schema_violations` retains `error.absolute_path` and a scalar constraint only (`pipeline_planner.py:2755-2779`). A `required` error locates the parent, while its constraint is a list, so a missing `nodes[0].input` is reported as path `nodes/0`, rule `required`, without naming `input`. The dispatch schema producer already demonstrates how to identify absent schema-owned names without echoing rejected values (`tools/_dispatch.py:594-605`).

**Counterevidence and limits:** The discovery failure retains `success=False` and keeps state validation separate from argument validation; it does not falsely claim a graph mutation. Rejected discovery calls roll back request bookkeeping so a corrected retry remains possible (`pipeline_planner.py:5027-5034`). Wrapper guidance is retained. This is reduced actionability, not proof that a model cannot recover or that any particular session looped. The parser/schema still supplies correct rejection authority.

**Minimal guarded option:** Extend the owned planner argument response with bounded canonical schema locations and closed violation codes already admitted by the exception. For structural `required` failures, append only absent names read from the owned schema. Keep raw `input`, arbitrary mapping keys, validator prose, and causes out of provider context. No server-authored graph or broader schema-disclosure change is required.

## Strong existing controls and bounded path coverage

| Path inspected | Source-backed control or observed boundary |
|---|---|
| Actual validator → rejected candidate → planner repair | `_rejection_entries` selects the replacement's rejection instead of the unchanged baseline's phantom defects (`pipeline_planner.py:2181-2192`). Plugin-option detail is scoped to model-owned config, with typed plugin identities and policy-resolved contracts (`pipeline_planner.py:2567-2619`). |
| Schema/Pydantic → tool message | Closed locations and codes omit rejected values and mark over-budget diagnostics (`audit.py:1363-1492`). Freeform root-property, review-length and explicit failure-choice repairs are targeted (`tool_error_payloads.py:48-102`). Existing test expectations were not treated as independent semantic proof. |
| Review draft/stale arguments | `sessions.py:2607-2612,2672-2677,2760-2793` distinguishes current staged drafts from stale prompt/model/vague-term drafts. `sessions.py:329-378` validates the effective staged or inline draft; omission cannot bypass its content rules. Template metacharacters and control characters have separate producers (`sessions.py:311-325`). No obsolete 1024-line limit was inferred from history. |
| Interpreter/query/template errors | Expression grammar teaches function-call case folding, no coercion/default fabrication (`tools/generation.py:231-270`), consistent with inspected parser rejection sites (`core/expression_parser.py:349-368`) and transform assistance (`plugins/transforms/value_transform.py:694-697`). Template rules distinguish absent context names from undeclared inputs and skip syntax faults owned by plugin admission (`state.py:4045-4109`). This is sampled producer/consumer inspection, not an exhaustive grammar equivalence proof. |
| Source/blob inspection and refusals | Blob content pages carry original-byte hash, original character ranges, continuation offsets and explicit truncation (`tools/blobs.py:2055-2122`); the full text is credential-checked before slicing. A page that cannot fit returns a refusal rather than a successful empty inspection (`tools/blobs.py:2105-2106`). Source metadata/ready/storage failure branches are distinct (`tools/sources.py:820-888`). |
| Readiness/preflight/policy | Green, red, and unmeasured staging copy differ (`protocol.py:192-217`). Runtime repair preserves the active user's authorization and explicitly says read-only tools do not repair (`composition_completion.py:735-805`). Unauthorized secret wiring is identified as operator policy that Composer cannot authorize. Blob-bearing preflight cannot reuse a completed verdict solely on unchanged graph version (`composer_preflight.py:212-220,320-351`). |
| Time/token/cost/provider failures | Planner checks classify budget exhaustion separately from malformed responses; output length is handled before accepting a candidate (`pipeline_planner.py:3900-4005`). The advisor's pre-call deadline does not fabricate an exception/call (`tool_batch.py:2197-2223`); near-deadline refusals explicitly report `outbound_call_made=False` (`tool_batch.py:2225-2254`). Admission refusal requires a real refusal reason (`chargeable_admission.py:22-37`). |
| Cancellation/fences/failed operations | Cancellation and audit-integrity failures unwind rather than become argument-repair prompts (`tool_batch.py:2281-2287,2915-2918`). Preflight failures retain the newly applied partial state (`tool_batch.py:2994-3006`). Retained failures are authenticated and explicitly bounded; unknown attribution is not zero progress and no failure history does not prove prior success (`protocol.py:129-183`). This review did not requalify storage contention or operation fencing. |
| Advisor blockers/publication/history | A failed completion review preserves validated/pending/unmeasured readiness distinctions (`advisor_checkpoint.py:782-802,862-945`). Message rejection tells the user to reword rather than change a graph (`advisor_policy.py:453-474`). Model findings are fenced as data; unsupported reply claims can receive a prose-only correction (`composition_completion.py:2318-2342`). Control-row replay validates origin/role/hash instead of pretending backend feedback was human-authored (`control_messages.py:103-158`). |
| Frozen narrow provider projection | `turn_effects.py:451-474` scrubs authored text fields while retaining owned hashes/UUIDs/counts; `advisor_context.py:214-228` requires the nominal owned review and uses its provider projection with JSON-aware sentinel neutralization. This addresses the supplied whole-JSON digest-corruption mechanism statically. Private raw binding and provider projection are separate; this review does not certify all redaction paths. |

Withholding an original private cause is often correct. The standard is an honest public statement of the observation, plus a valid next action or explicit abstention. It is not verbatim preservation of every exception. In particular, a private cause, a safe public correlation value, a provider-actionable fact, and the user's visible message are different artifacts; none establishes the others' completeness.

## Bounded future evaluation set — not run

Use deterministic owner-level fixtures at the actual feedback serializer and loop consumer. No live-provider campaign is needed to establish the identified control-flow defects.

| Slice | Positive case and required result | Negative/refusal/privacy control |
|---|---|---|
| Progress versus repeated classifiers | Two coalesce conflicts, unrelated finalizer config, repair the first then the second; permit bounded remaining repair without an impossibility claim. | Truly unchanged semantic rejection still triggers the intended bounded no-progress action; private branch facts remain withheld. |
| Advisor provider taxonomy | BadRequest/API failure with no response yields neutral provider-failure wording and no fabricated response provenance. | An actually received invalid verdict still says malformed; concrete outage still says unavailable. |
| Composed retry disposition | Nonretryable auth/bad-request stops both automatic owners. | Transient failure and malformed-format re-prompt retain their allowed retries; no clearance is granted on failure. |
| Planner discovery arguments | Invalid `plugin_type`, type mismatch, bound mismatch, and unsupported root property retain distinct closed locations/codes. | An authored secret-like unknown key never appears in diagnostics; known wrapper errors still name the wrapper contract. |
| Required property localization | Omit `nodes[0].input`; feedback names the absent owned property. | Model-controlled mapping keys and rejected values remain absent; excess diagnostics have a visible omission marker. |
| Current versus staged review | Stale prompt/model draft, omitted staged draft, and valid multiline source artifact produce the appropriate exact/current/omission instruction. | Metacharacters, forbidden controls and credential-shaped content remain refused; no instruction recommends changing unrelated pipeline content. |
| Blob inspection claims | Multi-page original text, stale expected hash, and seam-spanning credential demonstrate correct coverage/refusal. | One page or metadata alone never establishes whole-file inspection, header-only content, or original-data preservation. |
| Runtime authority/readiness | Green graph plus unavailable advisor; pending handoff plus structural defect; unmeasured preflight. | No review is presented as deterministic validation; unauthorized secret wiring is never described as fixable by another Composer authorization call. |
| Lifecycle/retained effects | Cancellation, lost fence, and an earlier saved revision remain differentiated from the current request's satisfaction. | Failed persistence never claims saved progress; absent history never claims success or zero progress. |
| Frozen publication projection | A digest containing phone-shaped digits survives exactly while authored PII is scrubbed; renderer remains parseable and fenced. | Unowned review values are refused; redacted request text is never treated as proof that every original constraint was reviewed. |

Assess semantic correctness of the emitted feedback before evaluating whether a model follows it. A test that asserts today's message is not evidence that today's diagnosis or recovery instruction is true.

## Confidence Assessment

**Overall Confidence: Moderate.** Producer/consumer source chains are directly verified; behavioral incidence and the complete F1 scenario were not executed.

| Finding | Confidence | Basis |
|---|---|---|
| F1 repeat guard can conflate a new remaining conflict with impossibility | Moderate | Direct fingerprint/withholding/short-circuit chain; concrete multi-conflict consequence derived from the single-conflict producer. |
| F2 provider failure receives malformed-response provenance | High | Explicit classification, fallback and fixed terminal copy. |
| F3 outer re-review loses nonretryable disposition | High | Explicit inner break and outer loop conditions. |
| F4 safe localization is dropped | High for loss; Moderate for convergence impact | Closed producer facts and serialized planner carrier inspected; no model trial. |
| Narrow frozen production identity | High | Actual hashes matched the supplied freeze. HEAD identity remains supplied. |

## Risk Assessment

**Implementation Risk: Medium. Reversibility: Easy for wording; Moderate for retry/progress controls.**

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Expanding diagnostic detail reveals values/cross-component private facts | High | Depends on chosen patch | Reuse closed owned violations and existing custody tests; never reintroduce raw causes. |
| Weakening no-progress controls increases spend | Medium | Plausible if changed broadly | Distinguish semantic progress internally; retain existing request budgets. |
| Stopping all failed advisor re-asks removes valid transient recovery | Medium | High if disposition is collapsed | Carry an explicit retry decision; preserve formatting/transient controls. |
| Message/enum changes affect durable or public consumers | Medium | Depends on scope | Prefer neutral copy first; review compatibility explicitly before adding/changing persisted failure kinds. |
| Treating this review as release acceptance | High | Avoidable | Keep focused receipts, full CI, signing, integration and deployed acceptance under their existing owners. |

## Information Gaps

1. No deterministic execution of the F1 two-conflict/required-control scenario; that is the most useful confirmation before implementation.
2. No measured frequency, cost, model recovery rate, or production traces for these paths. No claims about an incident population are justified.
3. No independent Git HEAD/ancestry verification or full frozen-tree qualification; only the listed source hashes were measured here.
4. Provider/library runtime behavior and current remote documentation were not queried, under the no-network/no-import boundary. Source-defined classifications were reviewed as the application's contract.
5. Whole-redaction analysis is independently owned. This report does not resolve every possible change to request semantics during PII projection, nor requalify storage/fence races.

## Caveats & Required Follow-ups

**Before relying on a fix:** reproduce the targeted condition at its actual serializer and consumer, include the negative/privacy controls above, and re-read the touched production files after the integrator's tree changes. No full-suite result, protected CI, signing, merge, release, or deployment claim follows from this review.

**Assumptions:** supplied candidate HEAD and freeze belong to the release owner's intended candidate; source remains represented by the measured identities; no source mutation or provider experiment was authorized for this reviewer.

**Limits:** this is a finite initial review of representative end-to-end diagnostic paths, not a census of all plugin errors or a proof that every warning/error is accurate. Example behavioral outcomes are explicitly derived where unexecuted. Existing assertions and historical incidents were not substituted for present semantic evidence.

**Recommended next steps:** the release owner can address the confirmed F2/F3/F4 source defects in small changes with the controls above. Confirm F1's concrete convergence scenario deterministically before relying on that behavioral claim; its unsupported impossibility wording is directly established by source. Follow John's latest integration and full-local-gate instruction under the sole integrator's capacity ownership. Any implementation and release disposition remain with that owner; this reviewer made no changes beyond this artifact.

## Integrator disposition for the 0.8.6 candidate

The review above describes its recorded historical source boundary. All four
findings subsequently received bounded candidate repairs: provider failures
remain distinct from observed malformed responses (F2); nonretryable advisor
failures stop the outer automatic loop without charging unused passes (F3);
planner feedback retains closed schema categories, locations and missing
required fields without rejected values (F4); and the repeat guard distinguishes
a changed authored candidate from an unchanged repeated failure (F1).

The F1 progressive-repair consequence, unexecuted in the original review, was
subsequently reproduced with two successive coalesce conflicts before the
repair. Existing deadlines, pass limits, withholding and later-operation retry
behavior remain. Finite regression checks and regular source review do not
establish final local or exact-head CI qualification, provider acceptance,
operator signatures, publication or deployment.
