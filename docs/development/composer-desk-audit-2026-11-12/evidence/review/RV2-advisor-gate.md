# RV2-advisor-gate: adversarial review of F2, F8, F9, F16, F17, the advisor-parity report, R2, R3, R9, R14, R15, P2 and P3

Reviewer: RV2-advisor-gate. Tree: `45b65e50` (`git diff --stat 1a7ff0e6 45b65e50 -- src` is empty, so the code baseline matches).
Scope: README F2, F8, F9, F16 and F17; `evidence/advisor-parity-verification.md`; remediation R2, R3, R9, R14, R15, P2 and P3.
Mode: read-only. My scratch scripts are in the session scratchpad, not the repo.

How I checked:
- **EXECUTED.** Python over every `llm_calls.json`, `terminal.json` and `state.json` under
  `.claude/lanes/composer-desk-audit-20261112/live/sessions/` (L:) and `evals/composer-live-audit/runs/sessions/` (R:).
  That is 87 turns in all: 54 planner turns (50 completed, 4 failed) and 33 tool-loop turns.
- **Surface classifier.** A turn is a planner turn when its primary deepseek request offers `emit_pipeline_proposal`.
- **Advisor classifier.** An advisor call is a `z-ai/glm-5.3` call with no tools. A glm call that carries tools is the planner escape hatch and was excluded.
- **Instrument controls.**
  - The receipt grep matches 33/33 tool-loop turns and 0/50 planner turns, so it has both a positive and a negative control.
  - The `"not re-verified"` grep matches the 4 known planner withholds and 0 tool-loop turns.
  - The blob-ref probe returns True for the CSV, JSON and text sources and False for the s16 `llm` source.
  - The internals regex used for F17 fires on provider names in p01 T1, which shows it can match, and on nothing else.

## Verdict table

| # | Claim | Verdict | One-line reason |
|---|---|---|---|
| 1 | F2 mechanism: the advisor has the skill's "use the digest" rule but not the digest, so it withholds correct answers | CONFIRMED | Re-read all four withholds. The advisor reasoning says, for example, "no discovery evidence"; the planner answered from `authoring_aids`. The code sites check out. |
| 2 | F2 "4 of 23 informational (first) turns withheld" | CONFIRMED-WITH-CORRECTION | 4/23 reproduces exactly. But these are not "first turns", and 4 of the 23 are declined build requests, not informational questions. |
| 3 | F2 "nondeterministic; SharePoint 2/2, PDF 1/2, is-this-safe 1/2" | CONFIRMED-WITH-CORRECTION | The splits reproduce. The two PDF turns ran on different instances, and the two "is this safe" turns are different candidate replies. The R:s04 T5 withhold is grounded in `authoring_aids.prompt_shield`, which R2 does not supply. |
| 4 | F2: planner path has one review and no repair; `attempts = 2` retries only provider or parse failures | CONFIRMED | `planning_application.py:938-1010`, `advisor_checkpoint.py:1348`, `:1467-1470` |
| 5 | F2: withheld body is `render_saved_scope()` plus the "not re-verified" notice; `advisor_policy.py:451-455` wording is unused in chat | CONFIRMED | `advisor_checkpoint.py:958`, `planning_application.py:993-1002`, `turn_effects.py:37-39`, `no_tool_policy.py:207-212` |
| 6 | F8: receipt footer on every tool-loop reply | CONFIRMED | 33/33 tool-loop terminals carry "Checkpoint save status:", 0/50 completed planner terminals do. Skip condition is at `composition_completion.py:2632`. |
| 7 | F8: the sentence appears twice on a withheld tool-loop turn (s09 T2) | CONFIRMED | L:s09 T2 segments are text "covers 6 saved draft revisions…", then the handoff notice, then the receipt repeating it |
| 8 | F9: "Correction…" after an advisor repair | CONFIRMED | L:s02 T3 and L:s20 T2 replies start "Correction to my previous message" and "Correction: I misstated". The unpublished draft is an assistant turn right before the injection. |
| 9 | F16: on a valid, unchanged pipeline the gate re-checks only if the model calls `preview_pipeline` (HIGH) | OVERSTATED | It omits the blob-ref arm (`composer_preflight.py:356-368`), which recomputes every turn. 49 of 51 non-empty battery states carry `blob_ref`, so the None branch fired on 0 of 33 tool-loop turns. |
| 10 | F16 live evidence: "both uncleared tool-loop turns (s03 T4, s09 T2) were turns without `preview_pipeline`" | REFUTED | s03 T4 got the green *pending* branch (a computed, valid preflight). s09 T2 changed the pipeline (6 saved revisions) and got the pending-handoff branch. Neither is the `None` branch. |
| 11 | F16: Run stays enabled; gate facts withhold only `completion_ready` | CONFIRMED | `completion_gates.py:380-439`; `ExecuteButton.tsx:589-596` reads `/validate` `execution_ready` only. `L:s03 T4 validation.json`: `execution_ready: true, completion_ready: false`. |
| 12 | F16: "on a valid pipeline only a pipeline change or a model-initiated preview can clear it"; the "after your next pipeline change" copy is wrong | REFUTED (for the screenshot case) | The s03 T4 fact is `graph_rejected`. `graph_already_blocked` (`composition_completion.py:2260-2265`, `:2614-2620`) stops any pass on an unchanged graph, so a preview cannot clear it. The copy is accurate there. |
| 13 | F16: the panel repeats its re-run sentence | CONFIRMED | Visible in `evidence/advisor-gate-ui-s03.png` (detail plus suggestion) |
| 14 | F17: no Reply Register rule covers questions about the composer's own gates; the composer exposes internals | CONFIRMED-WITH-CORRECTION | The gap is real (`pipeline_composer.md:230-254`, READ). The incident has no trace, and 0 battery replies show it. Label it reported, not observed. |
| 15 | `evidence/advisor-parity-verification.md` | CONFIRMED-WITH-CORRECTION | The core analysis holds. Corrections: Q5 "is it safe" row; Q6 "four call sites" should be five; "26" receipts should be 33; the reused `catalog_scope` sentence. |
| 16 | README "What worked well": "Is this safe on sensitive data?" via `get_audit_info` | CONFIRMED-WITH-CORRECTION | True for L:s04 T5. R:s04 T5 called `get_audit_info` and was still withheld. That is the 4th case in F2's own count. |

Counts: 8 CONFIRMED, 5 CONFIRMED-WITH-CORRECTION, 1 OVERSTATED, 2 REFUTED.

---

## Details for verdicts that were not plain CONFIRMED

### #2: the 4/23 count (EXECUTED)

Definition: completed planner turns with no `emit_pipeline_proposal`, with the verdict taken from the glm-5.3 END call. That yields 23 turns:

| Session | Turns |
|---|---|
| L:c01 | T1, T2 |
| L:c03 | T1–T3 |
| L:p01 | T1–T4 |
| L:s04 | T1–T6 |
| L:smoke-edit | T1, T2 |
| R:s04 | T1–T6 |

Four were withheld, all `FLAGGED/WITHHOLD`:

- L:p01 T2 "Can you read PDFs?"
- L:s04 T2 "Can you connect to our SharePoint?"
- R:s04 T2 (same question)
- R:s04 T5 "Is this safe to run on sensitive data?"

**Corrections.**
- The bottom-line table says "4 of 23 informational **first** turns". None of the four is a first turn: they are T2 and T5. The planner handles every turn while the session is empty. Use "4 of 23 completed planner turns that ended in a prose reply". The F2 body already says "planner turns".
- c01 T1/T2 and smoke-edit T1/T2 are build requests the planner declined, not informational questions. Strictly, 4 of 19 conversational turns.
- **More useful rate for the demo.** Count the replies that make a deployment-absence claim with no tool evidence:
  - SharePoint ×2, PDF ×2, Teams ×1, prompt shield ×1;
  - 4 of 6 withheld.
  - Enumerations of available plugins ("What can you do?" ×2, "What kinds of steps…" ×2) were admitted 4/4, also with no tool evidence. The advisor only enforces *absence* claims.

### #3: nondeterminism and what R2 covers (EXECUTED)

- **PDF pair.** L:p01 T2 (WITHHOLD) and L:c03 T2 (ADMIT) ran on different instances (c03 is a `core-*` instance). The c03 reply claims "I checked the live policy-visible catalog", yet the advisor admitted it with "Informational Q&A… truthful". So the inconsistency is real, but the evidence was not identical.
- **"Is this safe" pair.** These are different candidate replies, not the same reply judged twice.
  - L:s04 T5 made no absence claim and was admitted.
  - R:s04 T5 asserted "There's no prompt-injection shield plugin implemented in this deployment".
  - The advisor flagged exactly that sentence (seq 13: "get_audit_info covers only audit configuration").
  - The planner took it from `authoring_aids.prompt_shield`: "No deployment implementation is selected." That phrase is present in R:s04 T5 seq 12 messages; source is `planner_authoring_aids.py:560`.
- **What R2 does and does not supply.** R2's proposed record has `available_ids`, `prohibited_ids` and `usable_profile_aliases`. It does **not** carry the `prompt_shield` fact. The advisor would have to infer "no shield" from the absence of `bedrock_prompt_shield`-type ids, which the reused `catalog_scope` text tells it not to do (see the R2 comments below). So at most 3 of the 4 counted withholds are in R2's reach.
- **Proof list.** R2's proof list omits R:s04 T5.

### #9 and #10: F16 scope and its battery instances (EXECUTED + READ)

**What the code actually does.** `reuse_or_recompute_runtime_preflight` (`composer_preflight.py:333-380`) returns `None` only when all of these hold:
- (a) `state.version == initial_version`;
- (b) no tool result this turn carried a `runtime_preflight`. That is `preview_pipeline` (`tools/generation.py:4606`) or a successful mutation's pending-review findings (`tool_batch.py:3268-3292`);
- (c) **no `blob_ref` anywhere in source, node or output options** (`:356-368`; `_contains_blob_ref` at `:59-65`);
- (d) the state is not wired-and-Stage-1-invalid (`:369-379`, which requires `state.sources and state.outputs`).

The README lists (a), (b) and a simplified (d). It omits (c) and the wiring condition in (d).

**What the battery shows.**
- Uploaded files and invented inline data are bound as blobs. `state.json` probe across all non-empty states: `src_blob_ref=True` on 49 of 51 states. The only exception is s16 (`llm` source).
- So in this battery the `None` branch could not fire on the tool loop. `"not re-verified"` appears in 0 of 33 tool-loop terminals.
- It appears only in the 4 planner withholds, where `runtime_preflight=None` is hard-coded (`planning_application.py:999`) over an *empty* session. That is an F2/R3 copy issue, not F16.

**The cited instances.**
- **L:s03 T4.** Zero tool calls, same state id `3383898b` at v11, source carries `blob_ref`.
  - The notice is "…Review the pipeline; validation and the advisory review run again after your next pipeline change". That is `_ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE` (`no_tool_policy.py:179-182`), the *validated_base* branch (`advisor_checkpoint.py:1000-1026`).
  - So a preflight **was** computed (blob arm), and it was valid.
  - The screenshot `advisor-gate-ui-s03.png` shows that same pending wording, not "not re-verified".
- **L:s09 T2.** 11 tool calls including `patch_node_options`/`patch_output_options`; version changed (6 saved revisions). So `state.version > initial_version` recomputed. The notice is the pending-handoff one ("A required interpretation review is pending…").
- **The reported session.** The turn was `upsert_node` + `set_output` + `request_interpretation_review`. It changed the pipeline, so it also recomputes. F16's mechanism does not explain that "did not clear". That was an advisor FLAG on a computed base.
  - The model's claim that `preview_pipeline` "is what satisfies the completion advisory gate" is its own theory. It matches the skill's terminal-state checklist (`pipeline_composer.md:1338`, "`preview_pipeline` returned `preview_is_valid: true`"), not the gate code.
  - No trace of that session exists: `docs/.../traces/` is empty, and a grep of the lane and docs finds only the README.
- **"Model skipped preview in 4 of 15 changed turns".** This does not bear on F16, because changed turns always recompute. By state-version change I count 4 of 16 on L: and 5 of 19 with R: included.

**Correction.** F16 is a real but narrow path: a non-blob source (`llm`, and possibly fetch/scrape-fed graphs), unchanged, no preview. Severity MEDIUM, not HIGH. Its "What happens / Live battery" bullet should be deleted. The reported session should be marked unexplained by this mechanism.

**The precise effect the README misses (READ).** `AdvisorGatePassed`, which clears a persisted block, is recorded only when `graph_preflight_qualified` is true (`composition_completion.py:2582-2620`). That requires a non-`None` valid preflight or a verified handoff. A `None` decision preserves the prior fact (`completion_gates.py:268-269`). So on the narrow F16 path, **even a CLEAN advisor verdict cannot clear an earlier block**. That is the real "doesn't clear without preview" mechanism.

### #12: "only a pipeline change or a model-initiated preview can clear it" (READ + EXECUTED)

- The s03 T4 fact is `cause: graph_rejected` (`L:s03-gates/turn04/state.json` `composer_meta.completion_gates.advisor_signoff.cause`).
- `graph_already_blocked` (`composition_completion.py:2260-2265`) is true for an unchanged graph carrying a `GRAPH_REJECTED` fact. It suppresses `AdvisorGatePassed` (`:2617`) whatever the preflight. So a preview on the next unchanged turn **cannot** clear it.
- This is the 2026-09-22 ruling described at `no_tool_policy.py:199-206`. "Run again after your next pipeline change" is therefore accurate for the screenshot case.
- The README criticism holds only for the non-graph causes: `publication_rejected`, `message_rejected`, `unavailable` and similar.
- Side note: `advisor_block_covers_unchanged_graph` (`completion_gates.py:442`) is called only from tests. Production implements the ruling through `graph_already_blocked`, which still spends an END call on such turns.

### #14: F17 (READ)

- The Reply Register gap is real: `pipeline_composer.md:230-254`. It also says "If the user asks for the identifiers, give them" (`:253-254`), and R15 must be scoped against that.
- No battery reply shows tool JSON, "file a bug" or "advisory gate". The regex was controlled, firing on p01 T1 provider names.
- **Root cause the audit missed.** The composer cannot see why the gate blocked.
  - Its state context carries only Stage-1 `validation` (`prompts.py:421-427`). It does not carry `completion_gates`.
  - After a block, its replayed context holds only the fixed `ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE` (`advisor_policy.py:303-309`), with no reason and no next step.
  - So when it is asked "why?", it must guess. That is how you get "preview_pipeline satisfies the gate".

### #15: `advisor-parity-verification.md`

- **Q5 table**, row "Is it safe…": "passed in s04 t05 (called `get_audit_info`, ADMIT); a no-tool answer is SUSPECTED at risk". Since then R:s04 T5 **called the tool and was withheld**. The ungrounded part was the `authoring_aids.prompt_shield` fact. Update the row to CONFIRMED and add `authoring_aids.*` facts as their own at-risk class.
- **Q6 step 2.** "the four call sites" should be five. `composition_completion.py:949` also recomputes `review_candidate(...).binding` and compares it to `publication_binding`, on the advisor-repair replacement path. If R2 binds the snapshot hash and this site is not threaded, an admitted reply is replaced.
- **Q4 receipt count.** "all 26 tool-loop terminal results" is now 33/33 with the re-run.
- **Q6 step 1, "reuse the abandonment projection unchanged".** Its `catalog_scope` text reads "Available in this request; absence does not establish an engine capability gap" (`advisor_abandonment.py:116`). Reused verbatim on the END review, it argues *against* admitting "no SharePoint connector here". END needs deployment-scoped wording.
- Everything else I spot-checked holds:
  - the line citations in Q1–Q4;
  - the s04 T2 marker table;
  - seq numbers 4, 13, 31 and 33;
  - the s04 T4 history loss.

### #16: README "What worked well"

"Is this safe on sensitive data? via `get_audit_info`" is listed as good handling. The re-run of the same prompt is the 4th F2 withhold. Qualify the bullet, or move it.

---

## Remediation comments

### R2 (capability record for the END advisor)

The direction is right, and it is invariant-safe: server-owned evidence for review, no structure authored. It needs these changes:

1. **Thread all five `review_candidate` sites**, not four: add `composition_completion.py:949`.
   - Simpler and safer: give `TurnEvidence` the request's `PluginAvailabilitySnapshot` at construction. It already imports the type (`turn_effects.py:29`).
   - That way `review_candidate()` cannot be called with a mismatched snapshot, and the five bindings cannot drift.
2. **Do not reuse `catalog_scope` verbatim** (`advisor_abandonment.py:116`). Its "absence does not establish an engine capability gap" invites continued WITHHOLD on deployment-absence claims. Use "absent here = not available in this deployment for this request".
3. **Hallucination guard.** Ids evidence *presence, absence and prohibition only*. With the plan's sentence ("a presence/absence … claim consistent with it is evidenced"), a hallucinated *functional* claim passes, because `text` is a real id. Example: "Yes, the text source reads PDFs".
   - Today such claims also pass (enumerations were admitted 4/4), so R2 does not make this worse.
   - But the rubric sentence must say: "ids do not evidence what a plugin can do; option or format claims need a tool result."
   - With that, "yes, there is a SharePoint source" is still withheld (absent id), and the plan's proof holds for that case.
4. **Coverage.** R2 does not cover `authoring_aids` facts such as `prompt_shield`, so R:s04 T5 stays at risk. Either add the `prompt_shield.implementation_selected` boolean, or drop "is this safe" from the F2 claim that R2 resolves. Add R:s04 T5 to the proof list.
5. **Measured size.** About 0.85 KB for the 40 ids in the s04 digest without aliases, consistent with "≈1.1 KB". The digest has no `prohibited` key at all in this deployment.

### R3 (copy)

Good. Add the following:
- **(a)** The withheld replacement body renders as `kind:"text"`, attributed "ELSPETH said:" (L:s04 T2, L:p01 T2 segments). Make it a trusted segment. This is parity-doc defect (b), and it is not in R3.
- **(b)** "For informational turns with zero saved revisions, show no receipt": there is no deterministic "informational" signal. If the condition is just `saved_revision_count == 0`, the receipt is the only backend contradiction of a hallucinated "I've updated X" on a 0-revision turn when the advisor admits it. Prefer the details-toggle option over removal.
- **(c)** No R item addresses parity defect (e). After a withhold, the next planner context holds only the saved-scope sentence (L:s04 T4 `prior_assistant_messages[history_index=3]`), and the composer then said "I couldn't confirm a SharePoint connector" (L:s04 T4 reply). Add an item, or fold it into R3.

### R9 (correction wording)

Correct and minimal. Also change the injected instruction itself:
- It currently says "**Correct** the reply's unsupported claims … A prose-only **correction** is valid" (`composition_completion.py:2560-2561`).
- That primes "Correction:", as both reasoning traces show ("Let me correct the final reply"; "Let me correct the reply").
- Use "Rewrite the reply so it states only supported facts" plus R9's sentence.

### R14 (always recompute the preflight)

Needs rework before it lands.

1. **The proof is vacuous.** s03 T4 and s09 T2 never contained "not re-verified" (#10), so the replay passes with no change.
2. **It fixes neither observed symptom.**
   - The screenshot case is `graph_rejected`: still uncleared, by ruling (#12).
   - The reported mutation turn already had a computed preflight.
   - What R14 does fix is the narrow non-blob path, where a CLEAN verdict cannot clear a prior block (#9, last paragraph).
3. **Shared-rule side effects the plan ignores.** `reuse_or_recompute_runtime_preflight` is the shared rule for three consumers:
   - finalize (`no_tool_finalize.py:221`);
   - the preflight repair gate (`composition_completion.py:1383`);
   - the advisor gate (`:1747`, `service.py:2239`).

   Recomputing for every non-empty state therefore also does the following on question-only turns:
   - injects a cross-turn repair campaign ("Pre-finalisation runtime preflight found contract violation(s)…") over a Stage-1-valid but runtime-invalid state. One campaign per state identity, from the ledger at `:1416-1446`; these are the "hidden model turns with mutation pressure the user never asked for" that the ledger comment warns about;
   - appends red validator suffixes to explanations;
   - for half-built drafts, if "every non-empty state" includes unwired ones, reopens the e6ff1b8c13 repair spam that the wiredness scoping exists to prevent (`service.py:1907-1916`, `no_tool_finalize.py:111-116`).
4. **Cost and egress.**
   - A preflight is `validate_pipeline`: secret-reference resolution through the configured secret service (`execution/validation.py:522-524`), runtime plugin instantiation, and blob reads for source proof, on a worker thread with its own timeout.
   - A preflight exception raises `ComposerRuntimePreflightError` and fails the turn (`composer_preflight.py:218-268`). Today an unchanged non-blob Q&A turn cannot fail that way.
5. **"Cached per version" does not exist across turns.**
   - The composer cache is per compose call (`service.py:2514`) and is deliberately bypassed for blob-bearing states (`composer_preflight.py:215`, "Blob status changes without state version changes").
   - The only cross-turn memo is the execution service's (`execution/service.py:3350-3377`). It forces a fresh run for secret-ref sites.
   - A naive per-version cache would serve stale blob or secret verdicts.
6. **Misses another `None` source.** The review-handoff exit passes `last_runtime_preflight` straight through when the version is unchanged (`service.py:1883-1895` → `:2050`). It never calls `reuse_or_recompute`, so R14 as written does not reach it, and it ignores the blob arm.

**Smaller, safer variant.** Extend the existing cross-turn arm from "wired and Stage-1-invalid" to "wired" (`state.sources and state.outputs`).
- That makes non-blob states behave exactly as blob states already do in every battery session, which is evidence the behaviour is tolerable.
- Apply the same rule at `service.py:1883`.
- State the repair-ledger and secret-resolution consequences as an accepted decision.
- Keep "after your next pipeline change" for `graph_rejected`. Change the copy only for the non-graph causes.

The R14 invariant claim is correct: this is validation, not authoring.

### R15 (Reply Register)

Necessary but not sufficient.
- The composer does not see the gate's reason or its clearing condition (#14). Without that it will keep guessing.
- Pair R15 with a fixed, backend-authored "completion state" line in the state context: blocked-by-review, cause, and what clears it in product terms. Fixed copy only: no advisor prose, so no contamination. The "Ask the composer about this" button already quotes the reviewer note, but it does not say what clears the gate.
- Adding the case to the advisor END rubric turns leaky-but-useful answers into WITHHOLDs. Under today's copy that is F2's broken UX, and on the planner path there is no repair. Land R3 first, or leave the rubric out.
- Scope the rule against `pipeline_composer.md:253-254` ("If the user asks for the identifiers, give them").

### P2 (advisor caching)

Overstated; the proposed change is already implemented.
- The advisor system message is already the skill first, then trigger instructions (`advisor_checkpoint.py:453-454`).
- It carries an Anthropic cache marker on the skill block (`:491-495`), and per-turn evidence is already last, in the user message.
- **Measured (EXECUTED), cache hit as sum(cached)/sum(prompt):**

  | Surface | Hit rate |
  |---|---|
  | Advisor, L: | 51.5% |
  | Advisor, L:+R: | 52.6% (mean per-call 57.9%, median 61%) |
  | Primary | 90.2% |

- I could not reproduce the README's 45%.
- The skill prefix is a median 66% of advisor prompt chars, which is roughly the ceiling. The remaining gap is GLM-via-OpenRouter implicit caching plus the uncacheable evidence.
- Expect little from reordering. Shrinking the evidence, or a provider with explicit caching, is the lever.

### P3 (skip or lighten END on conversational turns)

Recommend against as written.
- "No capability claims" cannot be determined without classifying the prose. The finalizer explicitly does not infer claims from English (`no_tool_finalize.py:104`).
- "No saved effects" alone would have dropped real catches:
  - L:s03 T4 was a 0-revision explanation turn. The advisor found an undisclosed, unreviewed case-insensitive comparison in `split_travel`.
  - It would let every hallucinated capability answer through. It "fixes" F2 by removing the check.
- If a decision is wanted, the only deterministic candidate is "empty session and no tool calls". Even that still covers the capability Q&A that F2 is about.

---

## Missed issues

- **M1. The frontend gate display can stay stale after an unchanged-version turn (INFERRED, needs a browser check).**
  - s03 T4 persisted a new `advisor_signoff` fact onto the **same** state row (id `3383898b`, v11; `composer_meta` updated).
  - The frontend re-POSTs `/validate` only when the composition version changes:
    - `subscriptions.ts:470-511` returns early when `lastValidatedVersionBySession === version`;
    - `sessionStore.ts:533` clears validation only when `changed`;
    - the audit snapshot is also keyed by version (`useAuditReadinessSync.ts:28-41`).
  - So in a live tab the decision panel and the Share button keep the pre-turn verdict until reload or a manual validate.
  - The README's screenshot was probably taken on a reloaded session. It proves the reloaded state, not the live one.
- **M2. The real "cannot clear" mechanism for F16 is the `AdvisorGatePassed` qualification** (`composition_completion.py:2582-2620`, `completion_gates.py:268-269`): a CLEAN verdict over a `None` preflight preserves the prior block. See #9.
- **M3. The review-handoff exit bypasses `reuse_or_recompute`** (`service.py:1883`). With an unchanged version it hands `last_runtime_preflight`, possibly `None`, straight to the advisor gate, even for blob-bearing states. R14 does not touch it.
- **M4. No remediation for post-withhold context loss.** Parity defect (e), L:s04 T4: the composer loses its own correct answer and later contradicts it.

## Answers to the brief's specific questions

**Is there any other path that recomputes the preflight for a valid, unchanged state?** Yes.
- **Within a compose turn:** the blob arm (`composer_preflight.py:358-368`). It covers almost every real session, since uploads and invented inline data are blobs.
- **Outside the turn:**
  - `POST /validate` (`execution/service.py:3350-3377`, memoised and then `merge_completion_gates`);
  - the audit-readiness snapshot;
  - the run path;
  - post-compose persistence (`sessions/routes/_helpers.py`, but only when the version changed, `composer_turn.py:1166`).
- None of these feed the next turn's advisor gate. `last_runtime_preflight` and the preflight cache are both per compose call (`service.py:2514-2515`). The session store carries only `completion_gates` facts, not a preflight.

**Is "Run stays enabled" consistent with every frontend gate?** Yes.
- `canExecute` (`ExecuteButton.tsx:589-596`) reads `/validate` or the audit snapshot's `execution_ready`. `merge_completion_gates` never changes that axis.
- The other inputs are composer busy, session operation in flight, running, and pending interpretation cards.
- The turn's fully-blocking unverified `runtime_preflight` (`advisor_policy.py:156-219`, `execution_ready=False`) is not consumed by the frontend. A grep of `src/elspeth/web/frontend/src` for `runtime_preflight` finds only an error-reason literal.
- `completionOutcome.ts` derives "Pipeline ready" from `execution_ready` only, so the advisor gate does not affect that chip either. The README's "blocks … a 'completed' turn" should say what surface it means.

**Would R14 have a cost or side effect the plan ignores?** Yes; see the R14 comments, items 3–6.

**Would R2 let a hallucinated capability claim through?**
- Not an existence claim: "there is a SharePoint source" is absent from the ids, so it is still withheld.
- Yes for functional claims about a real id, unless the rubric sentence limits ids to presence, absence and prohibition. Today's advisor already admits such enumerations, so R2 is not a regression.
