# RV7: synthesis and plan review (README bottom line, method, severities, Not covered, remediation-plan.md)

Reviewer: RV7-synthesis-and-plan. Audit under review: commit `45b65e50` on
`ccr-d14093ba-glu6bw`. Every number below was re-derived from the raw captures
under `.claude/lanes/composer-desk-audit-20261112/live/sessions/` ("battery")
and `evals/composer-live-audit/runs/sessions/` ("later runs"), using
`summary.json`, `terminal.json` and `llm_calls.json`. Helper scripts are in the
session scratchpad: `palette.py`, `palette2.py`, `turns.py`, `perf.py` and
`rows.json`. Method tags: EXECUTED (I ran it and show the output), READ, INFERRED.

## 0. Ground truth I re-derived (EXECUTED)

| Set | Sessions | Turns | Provider calls |
|---|---|---|---|
| Battery (27 scenario dirs, excluding `smoke-edit`) | 27 | 70 | 258 |
| Battery plus `smoke-edit` | 28 | 72 | 264 |
| Later runs (s03 and s09 re-runs, s04 re-run, t01–t03) | 6 | 15 | 46 |
| `evidence/battery-turns.md` rows (battery plus s04 re-run) | 28 blocks | **76** | 273 |
| Everything captured | 34 | 87 | 310 |

- Battery status: 66 completed, 4 failed (s06 T1–T2, s10 T1–T2). 3 completed
  turns had their body replaced by the withheld receipt (p01 T2, s04 T2, s09 T2).
- Classified by the surface that actually ran: 41 planner turns and 29
  tool-loop turns. 14 of the 43 follow-up turns (turn index above 1) ran on the
  **planner**, because the session was still empty.
- The checkpoint receipt ("Checkpoint save status: …") appears on **29 of 29**
  tool-loop replies and on 0 of 41 planner replies. The planner figure is the
  negative control.

No subset of the captures gives **68** turns (README L19, L63, L480) or **74**
turns / "about 290 calls" (`tool-call-register.md:5`). The README's "27
sessions" fits the battery only, and the battery has 70 turns.

## 1. Verdict table

| # | Claim (location) | Verdict | One-line reason |
|---|---|---|---|
| C1 | "27 multi-turn sessions and 68 user turns" (README L19, L63, L480) | CONFIRMED-WITH-CORRECTION | The battery is 27 sessions and **70** turns (258 calls). The turn table shows 76. The register says 74 turns and ~290 calls. None of these is 68. |
| C2 | Performance table (README L482-488) | CONFIRMED-WITH-CORRECTION | Not reproducible. The "first turn (planner)" and "follow-up (tool loop)" columns mix turn index with surface (14 follow-ups were planner turns). By surface: planner n=41, p50 21.5 s, max 241.6 s; loop n=29, p50 23.6 s, max 109.6 s. The loop p90 is 61, 85 or 103 s depending on the percentile method, because n=29 with a long tail. |
| C3 | "edits … produced valid graphs in 12–40 s" (README L19-21) | OVERSTATED | 5 of 29 tool-loop turns took more than 40 s: s02 T2 109.6 s, s09 T2 103.4 s, s16 T2 84.6 s, s12 T2 55.1 s, s02 T4 42.1 s. s02 T2 is a runbook "Safe" prompt. `valid` was also measured **after the driver auto-accepted every review card and proposal** (battery-turns.md header), so presenter card time is not in these figures. |
| C4 | Bottom-line F2 row: "4 of 23 informational first turns withheld" (README L30) | CONFIRMED-WITH-CORRECTION | 23 reproduces only if the 2 `smoke-edit` and 2 c01 declined-build turns are counted. The withheld turns are not first turns (s04 T2 and T5 are turns 2 and 5). The F2 body (L142) says "informational planner turns", so the two places disagree. |
| C5 | "PDF 1/2" (README L30, L143) | CONFIRMED-WITH-CORRECTION | The admitted PDF run (c03 T2) was on the **default-allowlist** instance and the withheld one (p01 T2) on the broad instance. The two digests differ, so this is not a like-for-like repeat. |
| C6 | Runbook: "Can you connect to X?" is "a coin flip" (plan L146) | OVERSTATED (as reassurance) | SharePoint was withheld 2 of 2 (s04 T2 battery, s04 T2 re-run). That is reproducible, not a coin flip. |
| C7 | Runbook "Safe, all validated live" list (plan L133-143) | REFUTED for 3 of 9 prompts | (a) "Is this safe to run on sensitive data?" was withheld in the s04 re-run T5, with the body replaced by the receipt plus "did not clear … not re-verified". (b) "Why did you choose that design?" (s03 T4) ended with "[ELSPETH-SYSTEM] Completion advisory review did not clear after the available attempts. Composer completion is withheld." (c) "Also put the complaints in their own separate file" (s02 T2) took 109.6 s and 9 calls, including a recovery call. |
| C8 | "What worked well: … 'Is this safe on sensitive data?' via get_audit_info" (README L515-517) | REFUTED (internally) | F2 lists the same prompt as withheld 1 of 2. |
| C9 | "Identifier-level teaching is clean" (bottom line, L21-23) | CONFIRMED-WITH-CORRECTION | Literally true. In the bottom line, though, it reads as general teaching health, which F5, F6 and F7 contradict. Say "identifier-level" and point to F5–F7 in the same sentence. |
| C10 | F7: "66 of 110 planner calls (60%) spend reasoning working out which taught tools they lack" (README L248; crosscheck L51) | OVERSTATED / UNVERIFIABLE | The instrument was not preserved; `mine_sessions.py` holds only the much broader CONFUSION regex. A strict instrument (a sentence that names one of the 23 unoffered tools **and** says it is unavailable; controls: positive matched, negative not) gives **24 of 110 (22%)**, 7 of them in s10. A broad instrument that accepts any mention of "proposal-only surface" or "palette" gives 67 of 106 (excluding smoke). The 60% depends on the instrument. Many hits are the model correctly orienting to the bridge sentence, not working around a gap. The 110 also includes the smoke-edit turns. |
| C11 | F16 live evidence: "Both tool-loop turns that ended with an uncleared advisory gate (2 of 33: s03 T4, s09 T2) were turns without preview_pipeline" (README L352-355) | REFUTED as evidence for F16 | Both were **substantive FLAGGED verdicts**, not the "unverified" branch. s03 T4 advisor: `FLAGGED/prompt_defect`, because the case-insensitive `travel` match was an authored choice with no review card. s09 T2: `FLAGGED`, with three pending requirements and a pending review. Neither terminal contains "not re-verified". That text appears **only** on the 4 F2 planner withholds (grep over all terminal.json; control "did not clear" hits 6 files, including s03 T4 and s09 T2). R14 would not have cleared either turn. The denominator "33" also uses all runs (29+4), while the rest of the README uses 68. |
| C12 | F1 narrative: "After three rejected proposals (repair budget 2), the planner escalated…" (L112-114) | CONFIRMED-WITH-CORRECTION | True for T1 (emits at calls 6–8, then a GLM 23-tool call of 69.2 s). T2 had **two** emits (calls 6–7), then a GLM call of 70.6 s, plus a 55.6 s call with no tools. |
| C13 | "escape hatch" terminology (README L48 "3 escape-hatch tools"; F1 L112 "escape hatch … full 23-tool palette"; register L14 "Hatch: the planner's escape-hatch turn") | CONFIRMED-WITH-CORRECTION | These are two mechanisms. (1) Advisor-model escalation on the planner: GLM 5.3 with 23 tools (s10 T1/T2, s16 T1). (2) A 2-tool recovery call (`handoff_to_user`, `restore_parked_graph`, DeepSeek) on **tool-loop** turns (s02 T2, s03 T3, s09 T2, s12 T2, s15 T2, s16 T2, s17 T2). F1 calls (1) "the escape hatch"; Method and the register define the hatch as (2), as a planner surface. |
| C14 | F1 "2/2 failed" (bottom line) | CONFIRMED-WITH-CORRECTION | This is 1 session. T2 is the same empty session after T1 failed. The structural cause is model-independent (READ: `sessions.py:2188`, planner palette). The 240 s timeout path depends on the model. Write "1 session (2 turns)". |
| C15 | Severity F1 = DEMO-BLOCKER | CONFIRMED (unscripted) | It is certain on a natural phrasing. The presenter controls attachments, so the runbook removes it. Add "empty session only; attaching to a non-empty session is untested". |
| C16 | Severity F2 = DEMO-BLOCKER | CONFIRMED | This is the one blocker a runbook cannot control if the audience asks questions live. The cause is model-independent (READ). The withhold rate is GLM-specific. |
| C17 | Severity F4 = "HIGH (DEMO-BLOCKER for a data-protection demo)" | CONFIRMED-WITH-CORRECTION | The bottom line (L37) already treats it as a blocker, so the dual label is inconsistent. The trigger is specific to DeepSeek `reasoning_content`: 2 of 2 in one session. Pick one label and add the model qualifier. |
| C18 | Severity F18 = DEMO-BLOCKER | CONFIRMED, conditioned | The suggestion fires deterministically on the tutorial graph (`state.py:10075-10081`: nodes present, no gate, no retaining error route). The presenter can avoid it, but **attendees doing the tutorial hands-on cannot**. The document never says whether the tutorial is presenter-led or hands-on. |
| C19 | Severity F8 = MEDIUM | UNDERSTATED for the demo | The receipt is on **29 of 29** tool-loop replies, so it is the single most-seen blemish under the runbook. F11 (one pipeline shape) is rated "HIGH (demo optics)". Calibrate F8 to HIGH (demo optics) and add it to the bottom-line table. |
| C20 | Severity F16 = HIGH | OVERSTATED on current evidence | Run stays enabled (EXECUTED in the audit's own screenshot). The battery shows no instance (C11). The reported session is not in the raw evidence. MEDIUM, from READ plus the report, until reproduced. |
| C21 | Severity F3 = HIGH; F3 "answers varied between runs" | CONFIRMED-WITH-CORRECTION | In r01–r03 nothing changed between accept and the question: state version is identical in T1 and T2 (r01 v2/v2, r02 v3/v3, r03 v4/v4). r02's "Nothing has changed" is **correct**, so r01–r03 do not show a wrong answer. Only s01 T6, after 4 edits, is a real F3 failure. |
| C22 | Severity F14 = HIGH (integrity) | UNDERSTATED in prominence | An approved interpretation that the run silently ignores falsifies the audit claim the product sells. It belongs in the bottom-line narrative as the top product (non-demo) risk, not between F11 and F16. |
| C23 | Severity F5, F6, F9–F13, F15, F17 | CONFIRMED | Labels fit the stated definitions. F11 needs a runbook line (see P-7). |
| C24 | "Deviations from production, which do not change any finding" (README L71) | OVERSTATED | The model pair, prompt guard mode and stores do change rates, latencies and possibly the F4 trigger. Say "do not change the code-level causes; rates and latencies are specific to this configuration". |
| C25 | "Not covered" (README L519-531) | CONFIRMED-WITH-CORRECTION | It says "Frontend rendering beyond the advisor-notice path" is not covered, but F18 is a browser walk. It is missing the items in §5. The single-model-pair caveat is buried in the last line. |
| C26 | Plan: WP1 items are "each small and independent" (plan L91) | REFUTED | R3 and R14 both edit advisor-gate copy and branch messages. R2, R9 and R15 all change advisor prompts or the END rubric. R9, R10 and R15 all change model-facing prompt text. They interact, and none can be proved without a joint live re-run (§4). |
| C27 | Plan: R14 proof "s03 T4 and s09 T2 replays: no 'not re-verified' text" (L29) | REFUTED (vacuous) | Neither turn contains that text today (C11). |
| C28 | Plan: R5 proof "lists the real change in r01–r03" (L22) | REFUTED (unsatisfiable) | There is no change to list in r01–r03 (C21). Use s01 T6. |
| C29 | Plan: R1 proof "planner reasoning about the missing palette falls well below 60%" (L18) | UNVERIFIABLE | The instrument behind 60% is not preserved (C10). |
| C30 | Plan: "None of the items routes around the provider or synthesises structure" (L9-12) | CONFIRMED-WITH-CORRECTION | True for R1–R15. R16(b) adds a tutorial-only UI branch that removes a composer-facing control. Invariant 2 ("no tutorial-only … code branch") needs an explicit operator ruling on it. The README states only that the composer path is untouched. R16(a) loosens the tutorial canary's admission (ADR-031). |
| C31 | Plan: R16 "make the chat line say 'Continue to Run' inside the tutorial" (L31) | OVERSTATED as a fix | The line comes from `frontend/src/stores/subscriptions.ts:459-460`. It fires on workspace `is_valid` after a pending review clears, not on tutorial readiness. Rewording it keeps a false readiness claim. In the tutorial shell, gate the message on `GET /api/tutorial/{id}/readiness` returning 200. |

Totals (31 claims):

- CONFIRMED 4: C15, C16, C18, C23.
- CONFIRMED-WITH-CORRECTION 12: C1, C2, C4, C5, C9, C12, C13, C14, C17, C21,
  C25, C30.
- OVERSTATED 6: C3, C6, C10 (also partly unverifiable), C20, C24, C31.
- UNDERSTATED 2: C19, C22.
- REFUTED 6: C7, C8, C11, C26, C27, C28.
- UNVERIFIABLE 1: C29.

C26–C31 are about the plan.

## 2. Evidence for the key non-CONFIRMED verdicts

**C1/C2 (counts and performance).** See `turns.py` and `perf.py`. Raw output:

```
battery70 planner 41 lat p50 21.5 p90 61.9 max 241.6 calls p50 3.0 max 9
battery70 loop 29 lat p50 23.6 p90 61.0 max 109.6 calls p50 4.0 max 9
battery70 n 70 calls 258 | live72 n 72 calls 264 | table76 n 76 calls 273 | all87 n 87 calls 310
by turn index: battery70 first 27 p50 26.3 p90 62.0 max 241.6 / follow 43 p50 18.2 p90 52.5 max 167.7
loop sorted tail: … 42.1, 55.1, 84.6, 103.4, 109.6
```

Correction: state "70 turns in 27 sessions (258 provider calls), plus a
6-turn s04 re-run, 4-turn s03 and 2-turn s09 re-runs, and 3 tutorial-brief
turns (87 turns, 310 calls in all)". Label the performance columns "planner
turns" and "tool-loop turns", not first and follow-up. Publish the percentile
method. Use the same denominators in the register header.

**C7/C8 (runbook "Safe" list).**

- `evals/composer-live-audit/runs/sessions/s04-conversation/turn05/terminal.json`:
  "[ELSPETH-SYSTEM] Completion advisory review did not clear after the available
  attempts. Composer completion is withheld. Pipeline readiness was not
  re-verified this turn…". The advisor flagged the draft's claim that "There's
  no prompt-injection shield plugin implemented in this deployment". The
  planner digest did say `"prompt_shield":{"rules":["No deployment
  implementation is selected. "…`, so the composer was right and this is a
  genuine F2 instance.
- `live/sessions/s03-gates/turn04/terminal.json`: the system note quoted in C7.
  The advisor verdict was FLAGGED/ADMIT, a real catch on T3's authored
  case-insensitive match.
- `live/sessions/s02-feedback-classify/turn02/llm_calls.json`: 9 calls of
  27.0, 22.2, 9.1, 21.2, 9.9, 3.1, 1.3, 3.8 (recovery) and 5.1 s.

**C10 (60%).** `palette2.py` output: `planner 110 tool-lack sentences 24`,
per scenario `{'s10-two-files': 7, 's06-errors': 3, 's16-llm-source': 2, …}`;
`ctrl pos True ctrl neg False`. The broad variant (`palette.py`) returns
`planner calls 106 palette-hit 67`, and its sample hits are mostly "This is a
proposal-only surface: emit_pipeline_proposal alone in its turn". That is the
model reading the bridge sentence correctly. Correction: "between 22% (strict)
and about 60% (broad) of planner calls mention the palette mismatch; the strict
count is concentrated in s10". Preserve the instrument in
`evals/composer-live-audit/`.

**C11 (F16's live evidence).** `grep -rl "not re-verified" … --include=terminal.json`
matches only `live/…/s04-conversation/turn02`, `live/…/p01-capability-qs/turn02`
and the re-run's `s04-conversation/turn02` and `turn05`. The s03 T4 advisor
response (last call in `llm_calls.json`) is `{"verdict": "FLAGGED",
"category": "prompt_defect", "steps": ["split_travel"] … "publication_verdict":
"ADMIT"}`. Its advisor request contains `"runtime_preflight": {"is_valid":
true …` in the excerpt. Correction: remove the "Live battery" bullet from F16,
or restate it as "the battery has no instance of the unverified branch on the
tool loop; F16 rests on code reading and the 2026-11-13 report". Drop "s09 T2"
there, because "What worked well" (L512-513) counts s09 as a real advisor catch.
The "not re-verified" sentence does appear on **empty-session** planner
withholds, which R14's "every non-empty state" does not reach. Fold that copy
into R3.

**C21/C28 (F3 and R5).** The state versions are quoted in C21. The r02 reply
is "Nothing has changed since the version you accepted. The pipeline is still
at version 3…", which is correct. r03 T2 opens "Here is the corrected, verified
answer", another small F9-style instance.

## 3. Claims that need a "with this model pair" or sample-size qualifier

These claims present a battery observation as a product property. The
code-level cause (READ) in each case transfers to other models; the rate or
behaviour does not.

| Claim | Why it is model- or n-specific | Required wording |
|---|---|---|
| Bottom-line "Live" column (all rows) | One run per scenario with DeepSeek v4.1 Flash and GLM 5.3 via OpenRouter | Head the column "Live (DeepSeek v4.1 Flash + GLM 5.3, n shown)" and move L530-531 into the bottom line |
| F2 "nondeterministic"; 4 of 23 | The advisor's withhold propensity is GLM-specific; the missing digest is not | "With GLM 5.3 as advisor…"; keep the structural cause unqualified |
| F4 entirely | Depends on DeepSeek drafting sample credentials in `reasoning_content`; models that expose no reasoning, or summarise it, may never trip it | "With DeepSeek v4.1 Flash, 2 of 2 turns in one session" |
| F7 "60%", "mostly converges" | Instrument-dependent (C10) and model-specific | See C10 |
| F1 "2/2", 240 s | n=1 session; the timeout path depends on model latency | "1 session; unbuildable on any model (READ)" |
| F5 "In s08 the same model reasoned the opposite" | One model | Fine as an anecdote, but label it as one |
| F9, F10 | Reasoning-trace evidence from one model | "Observed with DeepSeek" |
| F16 2/33 | Not evidence (C11) | Remove |
| F18 "3 of 3 without Apply"; "1/1 with Apply" | n=3 single-turn API runs and 1 browser session (tut1, tut1b and tut1c resumed the same session) | Keep, and say "DeepSeek, n=3" |
| Performance table and P2 "45% vs 90%" cache | Caching and latency are OpenRouter- and provider-specific | "On OpenRouter; re-measure on the demo provider" |
| "Tool-loop edits are reliable" | One run each across 9 sessions | "worked in every single run we made" |
| "The tool loop works" | Same | Same |

## 4. Remediation-plan review

### Dependencies

- **R1 before R4 and R8 is only partly true.** R4(c) (teach a hand-off)
  needs planner-specific text, so it depends on R1. R4(a) (named sources
  carrying a `blob_id`), R4's no-progress guard (P5) and R8 (filtering digest
  prose at render time) do not depend on R1 and can go in either order. E3 must
  follow R1, because today it would fail at 109 mentions.
- **R1 leaves the advisor's surface unspecified.** The advisor receives the
  same skill as "binding local policy" and reviews both planner and loop turns.
  The plan must say which rendering the advisor gets. Otherwise the advisor
  keeps judging planner turns against loop-only rules.
- **R1 and P1 may cost caching.** Today the three roles share one byte-identical
  prefix, which is part of why caching reaches 88–93%. Splitting it can lower
  cache hits. P1's "fewer tokens" claim needs a measured cache comparison.
- **R2 and R15 both edit the advisor END rubric; R3 and R14 both edit the gate
  copy.** Sequence each pair, or land each pair as one change.

### WP1 "before the demo" risk

About a week before an 80-person demo, with one maintainer:

| Item | Before demo? | Reason |
|---|---|---|
| R16(c) 409 detail, plus the tutorial readiness-message fix (C31) | **Yes** | Tiny and contained, and it removes the dead end's confusion |
| R3 withheld and receipt copy | **Yes** | The most-seen blemish (29 of 29 loop replies). It is copy plus a details toggle, and tests pin wording, so the blast radius is visible |
| R9 | Yes (XS) | One sentence in the repair instruction; check it in the joint re-run |
| R11 (`report_assemble.output_semantics()`) | **Move in from WP4**, or add a runbook line | The runbook enables `report_assemble` and `sink:document`, which is the shape that shows an uncleared HIGH warning (F11) |
| R2 | **No**, unless the demo cannot avoid capability Q&A | It changes advisor evidence on every turn and touches whole-tree wire-shape and redaction gates (AGENTS gotcha). Its proof needs 15 or more live repeats plus a regression battery. It changes the persisted evidence shape, so the delivery posture needs a compatibility note. If it is skipped, the runbook must handle audience capability questions offline |
| R14 | **No** | It changes shared preflight behaviour, which needs the full suite under AGENTS. The battery shows it would not have changed any observed turn (C11), and Run is not blocked. Fix the copy via R3 instead |
| R10, R15 | **No** | They change prompts on the headline flow without fixing any visible demo failure. They are behavioural risk without demo benefit |
| R5 | Optional | The runbook avoids "What changed". If it lands, use the s01 T6 proof |

### Invariants and posture

- R16(b): hiding Apply only inside `TutorialFreeformShell` is a tutorial-only
  branch that changes what the learner can send to the composer. AGENTS says
  "If you believe you need an exception, STOP and ask". Mark it as needing a
  ruling, not as "UI-only; composer untouched".
- R16(a): widening the tutorial admission weakens ADR-031's fixed-script
  canary. That is a decision, not a fix.
- R6(a): dropping reasoning from persistence and replay removes audit evidence
  for that turn. The decision record should say so explicitly. The audit grade
  is a product characteristic (ADR-046).
- R4(a): changing the canonical `sources` shape changes a persisted, public
  Composer document. Under the 0.8.11 delivery posture this needs a stated
  compatibility boundary. The 0.8.7 exception does not cover it.
- E2: a generated, gated register with live columns overlaps
  `composer_wire_census` and the skill inventory. AGENTS' "keep a gate only
  when it materially improves…" test favours E3, the cross-check CI test, over
  E2. Mark E2 as marginal.
- P3: "no capability claims" is decided server-side from reply content, which
  is a heuristic classifier. It would also mask F2 by skipping the advisor.
  State that interaction.

### Missing from the plan

- **Owner, date and go/no-go gate.** There is no code-freeze date, no named
  owner per item, and no abort criterion (for example, "if the preflight
  battery on the demo host has any failed or withheld turn among the runbook
  prompts, cut that segment").
- **Joint verification.** One combined live re-run after all WP1 prompt and
  copy changes, on the **demo model pair and host**, with repeats (at least 3)
  for the runbook prompts. Today each item's proof assumes it can be measured
  in isolation.
- **AGENTS gates.** R14, R2 and R1 touch shared runtime behaviour or wire
  shapes, so they need the full `pytest tests/` (via
  `scripts/full-suite-gate.sh`) and the whole-tree gates. Budget that time
  before the demo.
- **Rollback.** WP1 changes need a pre-demo build tag to fall back to.
- **Runbook preflight additions.** A tutorial browser walk, not just API
  scenarios. A tutorial **run** stage, which checks `web_scrape` egress to
  `dta-au.github.io` from the demo network. Prompt guard in `required` mode,
  which the battery never ran. The demo model pair.
- **Runbook timeout advice.** "Consider raising the timeout" lengthens the F1
  spinner on any failure. Pair it with the advice to avoid multi-file prompts,
  or drop it.

## 5. What an SES-level audience would ask that the document cannot answer

1. **Which model and provider will the demo run on, and was anything here
   reproduced on it?** The battery used DeepSeek and GLM via OpenRouter.
   "Not covered" excludes Anthropic and Bedrock. If the demo runs on another
   provider, every rate and latency is unvalidated. The audience may also ask
   about the data path for these providers; say that only synthetic data was
   sent.
2. **Go or no-go?** There is no one-line recommendation, such as "Proceed on
   the runbook; land R16(c), R3 and R9; rehearse on the demo host by date X".
3. **What fraction of turns gave a good result?** For example: 66 of 70
   completed, 4 failed, 3 withheld, plus the gate notes. There is no headline
   success rate.
4. **Is the tutorial presenter-led or hands-on for 80 people?** This changes
   F18 from avoidable to near-certain. Concurrency under 80 users is not
   covered (`fundable_turns_estimate=16`, rate limits, timeouts).
5. **Has F14 already affected real pipelines or records?** Approved
   interpretations may have been ignored at run time. The document gives no
   scope query or statement.
6. **Who fixes what, by when?** Sizes are S, M or XS, with no days, owners or
   capacity statement for a single maintainer.
7. **Do the fixes weaken security?** R6 relaxes a credential control; R2 gives
   the advisor more data. These need a single "security impact" line each.
8. **Cost per turn and per demo.** Tokens are reported but not dollars.

## 6. Prioritised edits

### README

1. **Fix the counts everywhere** (L19, L63, L480; `tool-call-register.md:5`):
   70 battery turns, 27 sessions, 258 calls, plus the later runs (87 turns, 310
   calls). Re-run the performance table split by surface, state the percentile
   method, and label the columns "planner turns" and "tool-loop turns".
2. **Bottom line.** Add a qualifier sentence ("one run per scenario, DeepSeek
   v4.1 Flash composer and GLM 5.3 advisor via OpenRouter; rates are
   indicative"). Replace "12–40 s" with the measured range (10–110 s, with 5 of
   29 above 40 s). Note that `valid` is after auto-accepting the cards. Add F8
   (receipt on 29 of 29 follow-up replies) to the table. Add a go/no-go line.
3. **F2 row and body.** Use "4 of 23 completed empty-session turns that
   produced no proposal (19 questions plus 4 declined builds); SharePoint 2/2".
   Note that the PDF pair ran on different allowlists. Use the same wording in
   both places.
4. **"What worked well."** Remove "'Is this safe on sensitive data?' via
   get_audit_info". Qualify "reliable" as "every single run".
5. **F16.** Delete or restate the "Live battery" bullet (C11). Downgrade to
   MEDIUM until reproduced. Note that the "not re-verified" sentence appears on
   empty-session withholds and belongs under F2/R3.
6. **F7.** Replace "66 of 110 (60%)" with the strict and broad pair and commit
   the instrument. Change "root cause behind F1" to "contributing cause; F1's
   root cause is the planner's inability to bind multiple blobs (R4)".
7. **F3.** Say that r01–r03 had no change after accept, so r02's "nothing
   changed" was correct. Cite s01 T6 as the real failure.
8. **F4.** Use one severity label. Qualify it by model.
9. **F1.** Write "1 session (2 turns)". Note that T2 had two rejected
   proposals. Distinguish "advisor-model escalation (23 tools)" from the
   "recovery hatch" (2–3 tools, tool loop). Align Method L48 and register L14.
10. **Severity order and presentation.** Put the findings in numeric order or
    group them by severity (today: F11, F14, F16, F17, F18, F15, F12, F13).
    Raise F14 into the bottom-line narrative as the top product-integrity risk.
11. **"Deviations … do not change any finding"** becomes "do not change
    code-level causes; rates and latencies may differ".
12. **Not covered.** Remove "frontend beyond advisor-notice" (F18 walked the
    tutorial UI) or narrow it. Add: the demo model and provider; concurrent
    multi-user load; review-card time (auto-accepted); the tutorial run stage
    and its egress; prompt guard `required`; attaching files to a non-empty
    session. Move the single-pair caveat to the top.

### Plan

1. **Runbook "Safe" list.** Remove "Is this safe to run on sensitive data?".
   Mark "Why did you choose that design?" as liable to show the advisory note.
   Warn that "Also put the complaints in their own separate file" took 110 s.
   Change "(F2, a coin flip)" to "(F2; SharePoint withheld 2 of 2)". Add "avoid
   the many-rows → one-briefing shape (F11) unless R11 lands". Add a line on
   audience capability questions: answer verbally, do not type them.
2. **Rebuild WP1** as WP1a (before the demo): R16(c), the tutorial readiness
   message gated on tutorial readiness, R3, R9, and optionally R11. WP1b
   (after the demo): R2, R14, R10, R15, R5. Drop "each is small and
   independent". State the R2/R15 and R3/R14 overlaps.
3. **Fix the proofs.** R5 uses s01 T6, not r01–r03. R14 uses a deterministic
   unit test plus a reproduction of the 2026-11-13 session, not s03 T4 or
   s09 T2. R1 names a committed instrument with a threshold. Every live proof
   runs at least 3 repeats on the demo model pair.
4. **Add owner, date and gate columns** to the work-package table, a
   code-freeze date, a rollback tag and abort criteria.
5. **R16.** Mark (b) as needing an invariant-2 ruling and (a) as needing an
   ADR-031 ruling. Replace the copy-only chat-line fix with gating on tutorial
   readiness.
6. **Specify the advisor's skill surface under R1.** Add a cache-impact
   measurement to P1.
7. **Add compatibility and audit notes** to R4(a) (persisted `sources` shape)
   and R6(a) (reasoning dropped from evidence).
8. **Runbook preflight.** Add a tutorial browser walk including the run stage
   (egress), prompt guard `required`, the demo model pair, and a joint re-run
   after WP1a.
9. **Correct the dependency statement.** Only R4(c) and E3 depend on R1; R8,
   R4(a) and P5 do not.
10. **Mark E2 as marginal** under the delivery-posture test, and keep E3.
