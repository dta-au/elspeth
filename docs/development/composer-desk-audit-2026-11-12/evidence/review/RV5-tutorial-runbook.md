# RV5 — tutorial (F18 / R16) and demo-day runbook (WP0)

Reviewer: RV5-tutorial-runbook. Audit commit `45b65e50`; current tree = that commit (no `src/` diff vs `1a7ff0e6`).
Methods: EXECUTED = ran code or queried raw captures (scripts in the session scratchpad, read-only DB opens);
READ = traced code; INFERRED = reasoning only.

## Verdict table

| # | Claim | Verdict | Reason (one line) |
|---|---|---|---|
| 1 | F18: Continue to Run calls `GET /api/tutorial/{id}/readiness`, which runs `_tutorial_launch_blocker`, needing exactly one `sink:json` (`tutorial_service.py:154-215`, L174) | CONFIRMED-WITH-CORRECTION | L174 is right; the function spans **154-232**, not 154-215 |
| 2 | F18: Apply sends the suggestion to the composer as a user message (`ChatPanel.tsx:853-857`) | CONFIRMED | READ `ChatPanel.tsx:853-858`, `lib/suggestionPrompts.ts:57-58`; the persisted user message matches byte-for-byte |
| 3 | F18: the suggestion text comes from `state.py:10079` | CONFIRMED | READ; S1 fires on every graph with no gate and no retaining error route |
| 4 | F18: after Apply, the composer added a `failed_rows` json output and routed the source and all three steps to it; advisor CLEAN | CONFIRMED | EXECUTED: `composition_states` v2-v6 in `tut1/data/sessions.db`; capture rows 9 and 14 are `{"verdict":"CLEAN"...}` |
| 5 | F18: four cards (3 Acknowledge + 1 prompt review, Approve after View prompt) | CONFIRMED | EXECUTED: 4 `interpretation_events` resolved at 23:00:21/29/37 and 23:15:54; tut1b/tut1c logs |
| 6 | F18: Checks ✓ and the chat said "Every card acknowledged… Select **Run pipeline**…" | CONFIRMED-WITH-CORRECTION | Screenshot confirms it. The line is frontend-authored (`stores/subscriptions.ts:458-463`), not composer output, and the audit never cites its source |
| 7 | F18: the tutorial step has no "Run pipeline" button | CONFIRMED | READ: `TutorialWorkspaceFrame.tsx:47-51` (`completion:false`) and `runAvailable` default false, so `InlineRunResults.tsx:378-379` shows "No runs yet."; the walk logged `no_run_button` |
| 8 | F18: 409 `tutorial_plugin_set` with the generic text | CONFIRMED | EXECUTED: `web.log` 409 at 23:16:03; the in-process gate returns `tutorial_plugin_set` for v2-v11 and passes v1/v12/v13 |
| 9 | F18: without Apply, the composer chose discard with one output in 3/3, "and the tutorial would continue" | CONFIRMED-WITH-CORRECTION | 3/3 discard + single json is confirmed. "Would continue" was never exercised: no readiness call in t01-t03 and **zero runs** in the tutorial instance. Proven only up to the plugin-set and profile gates (EXECUTED in-process) |
| 10 | F18: ties the 2026-11-13 reported session (F16/F17) to this mechanism ("such as the preview/advisory explanation") | UNVERIFIABLE | No capture shows the reported session was a tutorial session; this is presented as an example of F18 without evidence |
| 11 | F18 severity DEMO-BLOCKER, "user-triggered, not random" | UNDERSTATED (scope) | The trap is deterministic and promoted: S1 appeared in 3/3 t0* states. Its Apply is the **primary** button whenever it is the only suggestion, and it is **counted** in "Awaiting your decision (N)" while cards are pending |
| 12 | Is the one-output rule deliberate and documented in ADR-031? | NOT DOCUMENTED | ADR-031 says nothing about the plugin set or output count. The rule exists only in code (`tutorial_service.py:172-196`), with no test or ADR pinning one output |
| 13 | R16 options (a)/(b)/(c) and "Do (c) regardless" | CONFIRMED-WITH-CORRECTION | (c) is right and invariant-safe. (a) is larger than "S": the run projection breaks with two sinks. (b) has precedent but misses the root cause. A smaller general fix exists (see Remediation) |
| 14 | Runbook: prompt guard `required` default; missing mounts give `composer_available:false` with that reason | CONFIRMED-WITH-CORRECTION | READ `config.py:303`, `service.py:701-715`. The text is in `composer_reason`. The mounts are `ELSPETH_WEB__COMPOSER_PROMPT_GUARD` and `…_COMPOSER_ADVISOR_PROMPT_GUARD`, which the runbook should name. **All 11 audit instances ran `debug-when-unconfigured`**, so no runbook prompt or timing was measured under the guard the runbook mandates |
| 15 | Runbook: plugin allowlist list | CONFIRMED-WITH-CORRECTION | `transform:report_assemble` and `sink:document` are already always-authorized (`plugin_policy/compiler.py:17-40`), which contradicts the audit's own F15. The runbook also never names the variable (`ELSPETH_WEB__PLUGIN_ALLOWLIST`) |
| 16 | Runbook: LLM profile + `DEFAULT_LLM_PROFILE` | CONFIRMED (tutorial part) | READ `tutorial_service.py:203-206`, `audit_readiness/service.py:256-258`. I did not verify "no LLM nodes can be authored" in general |
| 17 | Runbook: presenter needs the deployment-wide `user` role, else 403 `user_role_required` | CONFIRMED-WITH-CORRECTION | READ `auth/middleware.py:158-178`. The audit harness granted it with a **test fixture** (`instance.sh` → `tests.fixtures.identities.grant_test_pipeline_user`). The runbook should name the operator path (People & access → Roles / `identity_admin_routes.py:684`) |
| 18 | Runbook: timeout advice | CONFIRMED | `composer_timeout_seconds` has no default (`config.py:372`); `web.log` shows `composer_turn_budget_underfunded` at 240 s |
| 19 | Runbook preflight: re-run s01, s02, s03, s05, s09, s20; "every turn should complete and validate should pass" | OVERSTATED (as a gate) | That pass criterion **passes** s09 T2 (WITHHELD) and s03 T4 (advisory gate not cleared): both were `completed`, valid=True. It also excludes the tutorial (t01-t03 + browser walk) entirely |
| 20 | Runbook "Safe, all validated live" list | REFUTED (2 of 9 entries) | "Is this safe to run on sensitive data?" was **WITHHELD 1/2**. "Why did you choose that design?" ended with "Completion advisory review did not clear" **1/2**. The other 7 hold (counts below) |
| 21 | Runbook "Avoid" list | CONFIRMED | Consistent with the battery table; no counter-evidence found |
| 22 | Runbook tutorial: "build took about 40 s to the first card, and the tutorial needed 4 decisions" | CONFIRMED-WITH-CORRECTION | 40 s is right (22:49:51.7 → 22:50:32). The first card is the **proposal Accept**, so the learner makes **5** decisions: Accept + 3 Acknowledge + View prompt/Approve. t01-t03 also show accept + 4 reviews |
| 23 | Runbook tutorial workaround: "Remove the failed_rows output and discard failures instead." verified as one 28 s turn, then readiness 200 | CONFIRMED-WITH-CORRECTION | The turn is real: v12/v13 have one output, all discard; 23:19:36.3 → 23:20:02.7 ≈ **26 s** server-side. The **200 is not in any artifact**: no access log, no recorded response, zero runs. Only the plugin-set gate pass is reproducible (EXECUTED). n=1 |
| 24 | Runbook: "If the panel says 'Completion advisory review did not clear' on a valid pipeline, Run still works ('Advisory checks don't block Run'). Click Run." | CONFIRMED-WITH-CORRECTION | Run is **enabled** (validation.json `execution_ready:true` at s03 T4 and s09 T2). But the quoted string is the audit-readiness advisory note, not the completion gate. "Works" was never exercised: README "Not covered" excludes execution after Run |

Tally:

- CONFIRMED: **9** (rows 2, 3, 4, 5, 7, 8, 16, 18, 21).
- CONFIRMED-WITH-CORRECTION: **10** (rows 1, 6, 9, 13, 14, 15, 17, 22, 23, 24).
- UNDERSTATED: **1** (row 11).
- OVERSTATED: **1** (row 19).
- REFUTED: **1** (row 20, two of the nine "safe" prompts).
- UNVERIFIABLE: **1** (row 10).
- Row 12 answers the ADR question: the one-output rule is **not documented**.

---

## Non-CONFIRMED details

### 1. Line range
`grep -n` output: `154:def _tutorial_launch_blocker(`, `174: output_valid = …`, `232: return None`. Change "154-215" to "154-232" in README L402 and remediation R16.

### 6. Source of the "Select Run pipeline" line
`src/elspeth/web/frontend/src/stores/subscriptions.ts:458-463` is a global session-store subscriber with no tutorial context. Its own comment (L449-457) asserts that "Run pipeline" is "unique and searchable". That assertion is false in the tutorial frame. A second instance of the same defect is missed. While cards are pending in the tutorial, the decision panel headline reads "**Run pipeline is blocked.**" (`DecisionPanel.tsx:99-101`; seen in `tut1b.log` body "Awaiting your decision (1) / Run pipeline is blocked."). R16's copy fix should cover both.

### 9. "the tutorial would continue"
- t01-t03 made no readiness call (`post_turn_actions.json`: accept + 4 `resolve_review` only).
- The tutorial instance has `runs` = 0 in `sessions.db` and in `runs/audit.db`, and `data/outputs/` is empty.
- I EXECUTED `_tutorial_launch_blocker` in-process on the three t0* `state.json` files, with a MagicMock snapshot listing the 5 tutorial plugins and `tutorial_profile="standard"`. All three pass the plugin-set, availability and profile gates and stop at the mocked policy-readiness stage (`tutorial_required_core`, a mock artefact).
- Control: with `available=frozenset()` all three stop at `tutorial_plugin_unavailable`, and tut1 v2-v11 stop at `tutorial_plugin_set`.
- Reword to: "passes the plugin-set and profile gates; full readiness and the Run step were not exercised".

### 10. Link to the reported session
README L424-427 says asking the composer why "produces guesses, such as the preview/advisory explanation reported on 2026-11-13 (F16, F17)". No capture ties that reported session to the tutorial or to a second output. Either drop "such as…" or mark it as an untested hypothesis.

### 11. Severity scope (understated)
- `decisionPanelRows.ts:166-176`: every `validation_suggestions` entry becomes a decision row while anything blocks a verb. `count = rows.length` (L187) feeds "Awaiting your decision (N)" and the live region "N items need your decision" (`DecisionPanel.tsx:106-110, 204`).
- `DecisionPanel.tsx:375-400`: Apply is `variant="primary"` when `suggestionCount === 1`.
- t01, t02 and t03 `state.json` each carry exactly one suggestion, this S1. In the tutorial, the optional suggestion is therefore counted as a pending decision and drawn as a primary button, while the step header says "A decision is waiting for you in the chat. Answer it before you continue."
- The "1/1 in a browser walk" framing undersells this. The trap is offered on every tutorial build in the evidence (3/3 + 1/1). Note that the walk script (`opus/tutorial_walk.js`) clicked Apply only because `/^Apply/i` was in its button list. A human learner is pushed there by the count and the styling.

### 13. R16 options

See Remediation.

### 14. Prompt guard
- `grep -L composer_prompt_guard_debug_mode` over all 11 instance `web.log`s returns nothing, and 11/11 contain it. `evals/composer-live-audit/instance.sh:29` hard-codes the debug mode.
- README L73/L525 disclose this, but the runbook does not, even though it tells the operator to run `required`.
- Every "safe" prompt, every latency (including "40 s to first card" and "28 s" for the workaround) and the tutorial walk were measured without the Prompt Shields round-trip (default `timeout_seconds=10`, `prompt_guard_config.py:23`) and without its block decisions.
- The runbook should say so and require the preflight to run under the guard.
- Exact reason string: `"Composer is unavailable: required prompt guard configuration is missing for composer, advisor."` in field `composer_reason` (`app.py:2841`).

### 15. Allowlist
`plugin_policy/compiler.py:17-40` `REQUIRED_WEB_PLUGIN_IDS` already contains `sink:document` and `transform:report_assemble`. Drop them from the "add" list, or mark them as already authorized, so the runbook does not contradict F15. Name the variable `ELSPETH_WEB__PLUGIN_ALLOWLIST` (JSON list; `instance.sh:31`).

### 17. User role
The harness grant path is `tests.fixtures.identities.grant_test_pipeline_user`, a test fixture, so the audit never exercised an operator grant. Name the supported path: admin UI People & access → Roles (`cli.py:2732` wording; route `identity_admin_routes.py:684`).

### 19. Preflight gate is too weak
- s09 T2: `status=completed`, `validation_is_valid=True`, reply WITHHELD (battery table). s03 T4 (live): `completed`, valid, but `terminal.json` carries "Completion advisory review did not clear" (grep count 3; control 0 on T02/T03).
- "Every turn should complete and validate should pass" therefore certifies both visible failures.
- Add to the criteria: no reply starting "Verified checkpoint evidence covers" / "[ELSPETH-SYSTEM]", and no "advisory review did not clear".
- Add the tutorial: t01-t03 plus one browser walk through Run, Audit and Graduation.

### 20. "Safe" list (REFUTED for two entries)
Counts were re-derived from the raw `terminal.json` per run (`.claude/lanes/…/live/sessions/` and `evals/composer-live-audit/runs/sessions/`) and the battery table:

| Prompt | Live result | Verdict |
|---|---|---|
| feedback-export classify (one CSV) | c02, r01, r02, r03, s02 T1: 5/5 ok | holds |
| "Also put the complaints in their own separate file" | s02 T2 1/1 ok, **109.6 s, 9 calls** | holds, but flag ~2 min on stage |
| "Change the threshold to 500" | s03 T2 2/2 ok (12.6 s, 17.8 s) | holds (needs the s03 gate pipeline) |
| "Explain … in two sentences" | s01 T7 1/1 | holds |
| **"Why did you choose that design?"** | s03 T4: live **advisory gate not cleared**; rerun clean → **1/2 failure** | **REFUTED as safe** |
| "Undo that last change" | s01 T8 1/1 | holds |
| "What can you do?" | s04 T1 2/2 | holds |
| "Build me a pipeline." | s04 T3 2/2 | holds |
| **"Is this safe to run on sensitive data?"** | s04 T5: live ok, rerun (`evals/…/runs/sessions/s04-conversation/turn05/terminal.json`) **WITHHELD** ("Verified checkpoint evidence covers 0 saved draft revisions… [ELSPETH-SYSTEM]") → **1/2** | **REFUTED as safe** |

The F2 headline row itself says "'is this safe' 1/2". The runbook contradicts its own finding, and so does README L515-517 ("Conversational handling is good: … 'Is this safe on sensitive data?'"). Move both prompts to "Avoid until fixed" (F2, F16).

### 22. Decision count
`tut1.log`: `click_decision Accept` at 22:50:32, then 3× Acknowledge (`tut1b.log`) and View prompt + Approve (`tut1c.log`). Without Apply that makes 5 learner decisions. Reword: "one proposal Accept, then four review cards (three Acknowledge, one View prompt → Approve)".

### 23. Workaround
- EXECUTED: `chat_messages` shows the user text at 23:19:36.31 and the assistant reply at 23:20:02.00, with the advisor pass audit at 23:20:02.59. `composition_states` v12 (set_pipeline rebuild) and v13 have outputs `[project_brief_summaries]`, all `discard`. The in-process gate on v12/v13 passes plugin-set.
- `web.log` records only error envelopes (409s at 23:16:03 and 23:16:24) and no 200s. So "tutorial readiness returned 200" has no artifact behind it. Mark it as observed-not-captured, or capture it.
- On-stage risks in the sentence itself:
  - The output name `failed_rows` was chosen by the model in this run. Another run may call it something else, and today's 409 does not name it. Prefer "Remove the extra failure output and discard failed rows instead."
  - The fix was a full `set_pipeline` rebuild. In this 1/1 sample it raised no new review cards, but a rebuild can re-raise cards, giving another round of decisions before Continue.

### 24. "Run still works"
- `ExecuteButton.tsx:736-745`: "Advisory checks don't block Run." renders only for non-green **audit-readiness** rows (plugin trust, provenance, retention, secrets), not for the completion advisor. The correct evidence is the decision panel line "Share inspect link is blocked. Run pipeline is still available." (`DecisionPanel.tsx:101`) plus `execution_ready:true` in s03 T4 / s09 T2 `validation.json`.
- Both captured cases carry the **pending** wording ("Review the pipeline; validation and…"), not the "readiness was not re-verified" wording. The control: `no_tool_policy.py` has 4 hits for that phrase; both terminals have 0.
- The "unverified" builder sets `execution_ready=False` (`advisor_policy.py:155-219`). `merge_completion_gates` (`completion_gates.py:376-445`) restores it on a recompute.
- Say "Run stays enabled" rather than "works": execution after Run is listed as Not covered (README L522).
- Cross-slice note for the F16 reviewer: README F16 L345-354 attributes s03 T4 and s09 T2 to the `advisor_signoff_unverified_validation` branch. The captured wording in both is `_ADVISOR_SIGNOFF_PENDING_PUBLISHED_NOTICE`.

---

## Remediation comments (R16)

**Invariant 2 analysis.** `_tutorial_launch_blocker` is tutorial **run admission**, which is tutorial-specific by construction. It lives in `web/composer/` but is not on the composer authoring path. ADR-031 bans tutorial-only normalisation, planner shortcuts or server repair of the pipeline. Widening or rewording what admission accepts is none of those, and there is precedent: `_tutorial_control_transforms` (`tutorial_service.py:93-111`) already widened the accepted set when a fixed set proved unsatisfiable. So (a) and (c) respect invariant 2. (b) is UI chrome; precedents are `allowFork={false}` (`TutorialFreeformShell.tsx:240`) and `isTutorial` (`PipelineValidationSummary.tsx:45`). It does not touch the composer path either.

- **(c) Name the mismatch: do it, correct.** This is the minimum. The detail text is rendered verbatim (`TutorialFreeformShell.tsx:34-41, 206`). Make the output branch distinct from wrong transforms: split the L191-196 condition so that `not output_valid` returns its own message naming the extra output names. Use those names, not "failed_rows", so the fix holds for any model-chosen name. Proof: a unit test beside `test_launch_blocker_admits_this_deployments_control_transforms`.
- **(a) Admit a failure-retention json sink: larger than "S", with a hidden break.**
  - `_rows_from_artifacts` (`tutorial_service.py:620-650`) returns the **first non-empty row-format artifact ordered by `created_at desc`**. With two json sinks, if any row fails, the Run step can present the failure rows as the tutorial's result, depending on artifact order (INFERRED; not exercised).
  - `_count_discarded_rows` counts only `destination == "discard"`, so failures retained in the second sink vanish from the learner's discard count.
  - (a) therefore needs: a projection keyed to the primary sink, a failure-only-routes check (on_error / on_validation_failure / on_write_failure targets only), and frontend Run-step review (`TutorialTurn4Run*`).
  - Size: M. Record the admitted shape in ADR-031 or in the code's docstring; today the one-output rule is undocumented.
- **(b) Hide Apply in the tutorial shell: acceptable, but it treats the symptom.** The S1 suggestion is still visible to the model in every validation payload (the tool responses in `tut1` carry `suggestions`), and a learner can type the same request. (b) closes only the one-click path.
- **Smaller, general fix the audit missed (no tutorial branch at all):** stop treating optional suggestions as decisions everywhere.
  - Exclude `kind:"suggestion"` rows from `count` and from the "Awaiting your decision" title (`decisionPanelRows.ts:186-187`).
  - Render Apply as secondary always (`DecisionPanel.tsx:390`).
  - This is a product-wide UX correction: an "optional suggestion" (its own aria-label) should not be counted as a pending decision. It removes the steer that makes F18 likely, without any tutorial-special code. Pair it with (c).
- **Copy fix.** Prefer control-name-free wording in `subscriptions.ts:460-462` ("…your pipeline's ready to run.") over a tutorial-aware branch. Apply the same to `DecisionPanel.tsx:99-101` ("Run pipeline is blocked." → "Running is blocked."). If the copy must name the control, pass `isTutorial` the way `PipelineValidationSummary` does.
- **Proof as written** ("Continue to Run either proceeds (a) or the Apply control is absent (b); the 409 text names `failed_rows` (c)") should name the actual output, not the literal `failed_rows`. For (a), the proof should also complete the **Run** and check the projected rows come from the primary sink.

## Missed issues (in slice)

1. **The tutorial Run step was never exercised.** `runs`=0 in both DBs and `data/outputs/` is empty. Run is live: it scrapes `https://dta-au.github.io/elspeth/tutorial-site/project-{1,2,3}.html` with `allowed_hosts="public_only"` (`tutorial_sample.py:31`, `config.py:280-286`) and calls the LLM profile. The runbook config list omits that the demo host needs **outbound egress to dta-au.github.io**, or `ELSPETH_WEB__TUTORIAL_SAMPLE_BASE_URL` pointing at a reachable copy. The runbook's tutorial guidance stops at Continue to Run, with nothing on Run, Audit or Graduation.
2. **Runbook preflight omits the tutorial** although the runbook scripts it (row 19).
3. **The "Every card acknowledged" line can fire on a pipeline the tutorial will refuse.** It is gated only on `is_valid` after pending review (`subscriptions.ts:447-463`), not on tutorial readiness. Readiness is checked only when the learner clicks Continue.
4. **The ADR gap.** The one-`sink:json` / one-source / exact-transform-set contract is enforced only in code. ADR-031 states the tutorial uses "the same provider-backed authoring" path but does not record that tutorial admission is narrower than general validity. That gap is what turns a valid, safer graph (advisor CLEAN, Checks ✓) into a dead end.
5. **README L515-517 contradicts F2.** It lists "Is this safe on sensitive data?" as good conversational handling, while F2 counts it 1/2 withheld.
