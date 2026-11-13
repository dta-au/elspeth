# Composer remediation plan (from the 2026-11-12 desk audit, revised 2026-11-13)

Findings (F*) are in [README.md](README.md); the tool inventory is in
[tool-call-register.md](tool-call-register.md). Items are grouped as fixes
(R), performance (P) and enhancements (E), then sequenced into work packages
with gates. Subagent-ready lanes for these items are in
[work-packages.md](work-packages.md). This revision applies the seven-reviewer adversarial review
(`evidence/review/`).

Every proof that uses live behaviour runs at least 3 repeats on the **demo
model pair** with `evals/composer-live-audit/`. It is judged on the turn
table's deterministic labels (`turn_table.py`), not on prose.

Composer invariants apply throughout:

- the LLM authors all pipeline structure;
- there is no tutorial-only composer path.

R2 and R14 add server-side evidence or validation only. R16 is frontend-only:
it enforces the fixed script that ADR-031 already requires.

## Fixes

| ID | Fixes | Change | Size | Proof |
|---|---|---|---|---|
| R1 | F7 (contributes to F1) | Render the planner's skill per surface: the existing capability core plus a planner-specific overlay instead of the freeform tool-loop policy (mechanism: `capability_skill.py:155`). Also decide the advisor's skill text (`advisor_checkpoint.py:454`), a per-surface skill hash, and how the deployment overlay applies. The shared core legitimately names `set_pipeline` as the *document shape*, so the target is "no unoffered tool named as something to call", not zero mentions. | M | A committed instrument scans the **whole** outbound planner request (system, tool schemas, `authoring_aids`, `instruction`). Unoffered tools named as callable: 0. Strict palette-confusion rate (crosscheck pattern) below 5% over ≥3 runs of s01–s20. |
| R2 | F2 | Give the END advisor the capability facts the composer answered from. Store the plugin snapshot on `TurnEvidence` when it is built, so all **five** `review_candidate` call sites agree (including `composition_completion.py:949`). Render a compact "current deployment capabilities" record (available ids, prohibited ids, usable profile aliases; reuse `advisor_abandonment.py:109-129`), bound by snapshot hash. Add a rubric sentence saying the record proves existence, absence or prohibition of a plugin, not what a plugin can do. Keep the reused scope sentence unchanged: S1 measured it harmless, and the record must stay byte-identical to the abandonment path (CDA-07). | S–M | SharePoint, PDF, Teams and "is this safe" (L:s04 T2, L:p01 T2/T3, R:s04 T5) are admitted on ≥3 repeats each. A planted false claim ("yes, there is a SharePoint source") is still WITHHELD. |
| R3 | F2, F8, F20 | User-facing copy for withheld and receipt states. (1) Render a withheld replacement as a system notice, not "ELSPETH said:", using the `advisor_policy.py:451-455` wording. (2) Keep the checkpoint receipt, the only backend check against a hallucinated "I updated X", but restyle it as a collapsed or secondary line. Never show it twice (s09 T2). (3) Carry the withheld turn's context forward so the next turn does not lose its own answer (s04 T4). (4) Derive run-state copy (`DecisionPanel.tsx:99-101`, `subscriptions.ts:458-463`) from the screen the user is on. | S–M | Screenshot review of s04 T2, s01 T7, s09 T2. After a withhold, a follow-up question gets a consistent answer. |
| R4 | F1 | Make multi-file sources expressible on the planner. (a) Let named `sources` entries carry a session `blob_id`, resolved through the existing `_resolve_source_blob(source_name=…)`. Keep `inline_blob` out of named sources; define precedence against reviewed-source custody; reject `blob_id` together with `path`; update the pinned canonical field inventory. **Maintainer decision D2 (2026-11-13): approved; no compatibility.** Make it work with the new system only, with no migration or dual-shape reading for older states. (b) Until (a) lands: make the rejection name the problem ("named sources cannot bind uploaded files; bind one file per request or…") and teach the planner to hand off. | M (a), S (b) | s10 builds in under 60 s with the two-source combining topology, or hands off with a clear message within the turn. Test the review path for an LLM-authored blob on a named source. |
| R5 | F3 | Supply a baseline to tool-loop dispatch, and say in the wire result **which** baseline was used (request start, or last accepted). There is no recorded "accepted version" yet, so record one at accept/commit. Reword `diff_baseline_unavailable`, and stop reporting `is_valid: false` on a valid pipeline in that failure. A request-start baseline alone would turn an honest "I couldn't" into a wrong "nothing changed". | S–M | s01 T6 replay lists the GST/unit-price/filter changes since acceptance; r02's no-change case still says "nothing has changed". |
| R6 | F4 | **D1 approved (2026-11-13).** (c) **Now:** user copy that does not imply the user supplied a credential. (a) **Approved (D1; CDA-18):** when a hit is only in `reasoning_content`/`reasoning_details`, which the web tree neither replays nor stores durably, drop that reasoning from the call record, record the event, and continue. A hit in replayed Anthropic `thinking_blocks`, content or tool arguments must still fail. Applies on planner, tool-loop and advisor paths. This reverses behaviour pinned by `test_credential_guard.py:520-560`, `test_pipeline_planner.py` (~L5385) and `test_compose_loop_persistence.py:796-806`, and removes reasoning from the audit record for that call; both must be stated. | S | s06 T1/T2 build; a credential pasted by the user is still refused; a planted hit in tool arguments still fails. |
| R7 | F5 | No web configuration exposes `options.model`, so do not make the paragraph conditional. Teach in the skill that models are bound through operator profiles, and that changing one is an operator change, and remove the `model_catalog` reference. **D4 approved:** `list_models` returns each usable profile with its provider and model, and the composer names them when asked. | S | p01 T1, s08 T2, s02 T4 replies make no slug-pinning claim and cite no catalog. |
| R8 | F6 | Name only plugins available in the current policy snapshot: in the digest, the `authoring_aids` text and the on-demand hints. Otherwise say "an operator can enable X". | S | In-process: a default-policy planner request names 0 unavailable plugins. The live c01 check passes even today, so it cannot be the proof. |
| R9 | F9 | Reword the advisor-repair instruction. Drop "Correct the reply's…", and say: "The user has not seen your previous draft. Write the reply as the first and only message." | XS | s02 T3 / s20 T2 replays do not open with "Correction". |
| R10 | F10 | Reword `_ALL_INFORMATION_GAPS_CLOSED_NOTICE` as information, not a command. Append it at most once per request, and only when the model's own pending discovery (not just declared keys) is closed. 6 tests pin the string. | XS–S | ≤1 copy per request; s02/s09 inspect attachments before emitting. |
| R11 | F11, F15 | `report_assemble` declares `output_semantics()` with framing derived from configuration: `COMPACT` when the result is always one line, otherwise newline-framed (D3). `report_assemble` → `text` with a multi-line configuration becomes a validation error; today it passes with a warning and then writes nothing, because the text sink diverts newline-bearing values. Fix the `document` sink example, whose `schema.mode: fixed` errors after `report_assemble`. Review the same warning class on `json_explode` (s12). Correct the always-authorized list in `environment-variables.md` (9 vs 14). | S | s08 validates with no high warnings. (s14 writes json, so it is not a proof.) |
| R12 | F12 | Lead discovery failures with the tool's own error and omit whole-pipeline validation. For `canonical_schema`, name the offending key only when it is in the schema's closed vocabulary; this keeps the value-free design and covers all observed cases. Align `get_plugin_assistance` / `get_plugin_schema` argument naming in the next tool-contract revision; until then, state the name explicitly in both descriptions (no alias). For `patch_output_options`, say where `description` belongs. | S | Battery proposal rejections drop below 13 of 40. |
| R13 | F14 | At staging, reject a node-level `vague_term` slot on an `llm` node where any query with its own template would not receive it, or make the resolver write into every query template that carries the slot (larger). **Replace**, not append, the teaching at `planner_authoring_aids.py:867-870` and `multi_query.py:248-249`. Include the effective per-query prompt in the attested hash. | S (reject) / M (rewrite) | The in-process repro (`evidence/llm-wiring-verification.md`) is rejected at `set_pipeline`, or shows `accepted_in_prompt=True` for every query. Also audit whether stored records already carry the defect. |
| R14 | F16 | Recompute the turn-end preflight for every **wired** state (sources and outputs), matching today's blob behaviour, and apply the same rule at the review-handoff exit (`service.py:1883`). Separately decide whether a CLEAN verdict over a missing preflight may clear a prior block (`composition_completion.py:2582-2620`). **Costs:** each recompute resolves secrets and instantiates plugins. On question-only turns it can add repair turns or red suffixes on half-built drafts, and a preflight failure would fail a question. Measure before landing. | S–M | A deterministic unit test with a valid, unchanged, blob-free (`llm`-source) state yields a non-`None` preflight. A CLEAN verdict then clears. The s03 T4 / s09 T2 replays are not proofs. |
| R15 | F17 | Reply Register rule: answer questions about checks, readiness or approvals with the user's next action in product terms; never show tool-call JSON or ask the user to run a tool or file a defect. Give the composer the current blocker detail in its context, so it stops guessing. Add the case to the END rubric **after** R3, otherwise it adds withholds. | XS–S | Ask "why can't I share this?" on an advisor-blocked session ×3: the reply names the step and action, with no tool names. |
| R16 | F18 | **Make the tutorial Build step script-only, as ADR-031 intends.** Give `ChatPanelContent` a script mode (alongside the existing `allowFork`). In that mode the tutorial hides the free-text input and upload, suggestion Apply, repair-graph, "Ask the composer about this", inline-source edit and "Use as input". It keeps only the card actions the script needs: Accept the proposal, Acknowledge, View prompt, Approve. This is frontend-only: the composer backend and the brief are unchanged, so it neither adds a tutorial-only composer path (invariant 2) nor changes the canary. **Also:** the 409 `tutorial_plugin_set` detail names what differs; the tutorial chat line waits for tutorial readiness rather than workspace validity; and the e2e tutorial spec asserts those controls are absent. The earlier options are superseded: relaxing the one-output admission rule would weaken the canary, and demoting Apply everywhere does not stop typing. | S | Browser walk: after the brief, no control can send a composer message other than card decisions; brief → Accept → cards → Continue to Run reaches Run. The e2e spec fails if a free-text input or Apply appears in the Build step. |
| R17 | F19 | Make the boot probe send the real escape-hatch request (the 23-tool planner palette on the advisor endpoint), and correct its parity comment (`boot_probe.py:202-216`). Remove or wire up the dead `terminal_only` option (`capability_skill.py:193-219`). | XS | Probe output lists the hatch request with 23 tools. |

**Overlaps.** R2 and R15 both edit the END rubric, and R3 and R14 both
touch the gate copy, so land each pair together. All prompt and rubric
changes (R1, R2, R7, R9, R10, R15) need one joint live re-run, because the
items are not independent.

**Dependencies.** Only R4(b)'s hand-off teaching and E3 depend on R1. R4(a),
R8 and P5 do not.

## Performance

| ID | Change | Expected effect |
|---|---|---|
| P1 | Per-surface prompts (R1) shrink the planner's system text. The advisor half needs its own mechanism; decide it in R1. | Fewer planner prompt tokens; less reasoning spent working out the palette. Measure the cache impact, because the shared prefix changes. |
| P2 | Advisor caching is **already** structured (`advisor_checkpoint.py:453-454, 491-495`), yet the measured hit rate is 52% against 92–94% on the composer. Investigate whether per-turn evidence precedes stable text. | Lower advisor latency and cost. |
| P3 | ~~Skip the advisor on chat-only turns.~~ **Not recommended.** "No capability claims" cannot be decided without classifying the prose, and skipping would have lost the real s03 T4 catch. | — |
| P4 | Reduce proposal rejections (R12) and the "emit now" pressure (R10). In the battery 13 of 40 proposals were rejected, each a full planner call. | Fewer planner calls per first turn. |
| P5 | A wall-clock-aware planner exit: hand off before the operation deadline instead of dying at 504. The planner and operation deadlines are equal today, so add a reserve. Add a rule for output-cap truncation (`finish_reason=length`) in the hatch. | No 240 s spinners. |
| P6 | Address `composer_turn_budget_underfunded` (34 configured turns, about 16 fundable at 240 s): **D5:** for the demo, the presenter scripts turns to fit the configured budget; no code change. Size budgets to the timeout in the general product later. | Budgets that can actually be spent. |

### Composer efficiency: the hot-path recon and the plan of record

[composer-performance-remediation.md](../composer-performance-remediation.md)
is the plan of record for composer performance. Its fixes and S-/P- items
govern; nothing here supersedes them.

A separate hot-path recon reached us on 2026-11-13: `docs/development/hot-path-recon.md`
on branch `claude/admiring-bardeen-oj57gv` (commit `bf8adf06`), appendices C
and D. Its composer findings are **static reading, one reader each, not
cross-checked**, with line numbers valid at `1a7ff0e6`. They are leads to
measure, not measurements. The table maps each one to the plan of record and
to this audit.

| Recon finding (composer) | Plan of record | Interaction with this audit | Proposed action |
|---|---|---|---|
| Whole-graph `validate()` runs ≥3× per **mutating** tool call (`tools/_common.py:1756, :3849, :3956`). A rejected mutation pays 2; a read-only call pays 2 even on a discovery-cache hit. Each walk instantiates a plugin per node; about 65 ms for 50 nodes, linear. | S-6 covers discovery tools only | Every CDA lane that adds a tool call pays this | **P7:** extend S-6 to the mutation path: compute once per state version and carry the result forward (the governing rule). Profile `_check_schema_contracts` (`composer/state.py:4790`, the heavy half) within it. |
| Full Stage-2 validation runs after every valid mutation while reviews are pending (`tool_batch.py:3278`), and before every `preview_pipeline`, twice when handoff-shaped (`:2845`, `:2894`). It misses cache on each new version; `validation_memo.py` serves `/validate` and readiness only. | Fix 2B/2C memo (not the loop) | **CDA-09 (R14) adds more:** recomputing the turn-end preflight for every wired state puts Stage-2 on question-only turns | **P8:** measure Stage-2 per tool round. CDA-09 must reuse the per-turn cache and in-flight coalescing (`composer_preflight.py:152, :210-215`) and report the added cost. That is already in its brief; the cost now has a known size. |
| Each LLM round re-serialises the full context at least 4× and keeps one canonical copy per call, so CPU and stored bytes grow with rounds² per turn | P-14, Fix 4 | **CDA-12 (R1)** cut planner prompt tokens by 36% in S2 (first call −40%). Smaller context compounds here. | Fold into Fix 4/P-14. Measure P-14 again after CDA-12. |
| Each provider call is wrapped by 3 DB writes and 1 prompt-guard HTTP call; the Azure guard opens a new `httpx.AsyncClient` per inspection | S-4 (client reuse) | **CDA-20** retries multiply this per attempt | **P9:** batch the per-call bookkeeping writes, after S-4's client reuse. CDA-20 must keep each attempt auditable. |
| About 20–24 SQL statements per mutating call (9–11 otherwise), mostly the same fence proof repeated on one connection. A full state snapshot row per mutation, even when content equals the last. | Fix 13 (review-card path only) | — | **P10:** prove the fence once per transaction, and skip an identical snapshot row (**approved 2026-11-13**; CDA-23). |
| Deepcopy, canonical JSON and redaction run on the event loop shared with HTTP and every turn | S-3 | **S5's hands-on settings:** raising `composer_async_worker_concurrency` to 16 multiplies event-loop stalls for everyone | Runbook caveat (below). Raise S-3's priority if attendees will use the composer hands-on. |
| The freeform loop has no discovery-turn cap; `composer_max_discovery_turns` binds the planner only | — | **F21**, a behavioural finding (README) | **CDA-22** (D8 approved) |
| No composer or web path has coverage under `tests/performance` | — | — | **E7:** a composer validate benchmark (50-node baseline, about 65 ms) as a guard for P7/P8 |
| Advisor transcript rescans; credential scan per call; full transcript load per turn | S-8; Fix 11; S-5 | — | Covered by the plan of record; no new action |

## Enhancements

| ID | Change |
|---|---|
| E1 | Show the proposal's model-written `metadata.description` under the fixed first-turn sentence (F13). It keeps the composer invariant and the verdict constant. |
| E2 | Promote the tool-call register to a generated, gated artifact (next section). Marginal under the delivery-posture test; do it only with E3. |
| E3 | A CI test of per-surface teaching: across the **whole** outbound request for each surface, no unoffered tool is named as callable, and every offered tool is taught. This guards against "the skill says X but I don't have X". |
| E4 | Add the demo-shaped scenarios to `evals/composer-battery/corpus.md`: multi-file, capability questions, "what changed", security screening, "use GPT-4o", and the tutorial brief. |
| E5 | A one-sentence "Run it" hint naming the UI control. The composer answered "Run it." three different ways (r01–r03). |
| E6 | Rewrite `docs/reference/composer-tools.md` after R1. It documents 30 of 46 model-facing tools (`generate_yaml` is a CLI MCP export, not a web tool). This audit added the surfaces table and a pointer to the register. |

## Tool-call register: plan

1. **Generator.** Move `evals/composer-live-audit/build_register.py` and
   `render_register.py` to `scripts/cicd/composer_tool_register.py`. They read
   only live objects and contain no lint suppressions.
2. **Columns to add.** A per-surface rendered prompt (after R1), budget class,
   result shape (from `composer_wire_census`), and FRONTEND projection. The
   hatch reuses the planner palette on the advisor model; the reply stage has
   2 tools.
3. **Gate.** `--check` regenerates and diffs the static columns. Live columns
   come from the battery and are excluded from the diff.
4. **Ownership.** A change to a `TOOLS_IN_MODULE` tuple, a tool description or
   a skill passage regenerates the register in the same commit.
5. **Live join.** Run `evals/composer-live-audit/run_scenarios.sh` before each
   release candidate and before any external demo.

## Work packages, gates and order

Owner for every package is the maintainer; dates are set by the operator.

| WP | Contents | When | Gate before merge |
|---|---|---|---|
| WP0: demo config | Runbook below | Before the demo | Preflight below passes on the demo host |
| WP1a: low-risk copy and tutorial | R16 (script-only tutorial Build step, 409 detail, readiness-gated chat line), R3 (copy items 1, 2, 4), R9; optionally R11 | Before the demo, **only if** it lands before code freeze | Focused tests, frontend tests, one joint live re-run (≥3 repeats of s01, s02, s04, tutorial walk) |
| WP1b: advisor and gate behaviour | R2 + R15, R14 (+R3 item 3), R10, R5, R7 | After the demo | Full `pytest tests/` (shared runtime and wire shapes), testcontainer suite if persistence changes, joint live re-run |
| WP2: planner surface | R1 (+P1), R4, R8, E3, R17 | After the demo | Per-surface crosscheck instrument, s10 live, full suite |
| WP3: guard and integrity | R13 (promptly; integrity), R6 (after the decision) | R13 straight after the demo | Repro tests, full suite, audit of stored records for F14 |
| WP4: legibility and performance | R12, P2, P4–P6, E1 | Ongoing | Focused tests and battery comparison |
| WP5: register and corpus | E3, E4, E6 (E2 optional) | Alongside WP2 | `--check` gate green |

- **Code freeze:** set it at least 3 working days before the demo. After
  freeze, only WP0 changes.
- **Rollback:** tag the demo build. If any WP1a change regresses the
  preflight, revert to the tag.
- **Abort criteria on the day:** the preflight shows a FAILED or WITHHELD
  label on a safe-list prompt, or the composer reports unavailable. If so,
  present from recorded sessions.

## Demo-day runbook (WP0)

### Configuration (on the demo host)

- **Prompt guard.** In the default `ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE=required`,
  set both `ELSPETH_WEB__COMPOSER_PROMPT_GUARD` and
  `ELSPETH_WEB__COMPOSER_ADVISOR_PROMPT_GUARD` (Azure Prompt Shields).
  Otherwise `/api/system/status` reports `composer_available: false`.
  **None of this audit's timings were measured with the guard on;** preflight
  must be.
- **Plugin allowlist.** Set `ELSPETH_WEB__PLUGIN_ALLOWLIST` to the optional
  plugins the demo uses, at least `transform:type_coerce` and
  `transform:value_transform` for computed fields (F6), plus
  `transform:passthrough`, `transform:truncate`, `transform:keyword_filter`
  and the batch plugins if shown. `report_assemble`, `line_explode`,
  `reference_join` and `sink:document` are already in the required core.
  Restart, then check the `plugin_policy_readiness` rows.
- **LLM profile.** At least one `ELSPETH_WEB__LLM_PROFILES` entry plus
  `ELSPETH_WEB__DEFAULT_LLM_PROFILE`.
- **Users.** Presenter accounts need the deployment-wide `user` role (else
  403 `user_role_required`). Grant it through your deployment's
  administration path. The audit used a test fixture, which is not an
  operator procedure.
- **Network.** The tutorial's run step scrapes `dta-au.github.io`; the demo
  host needs outbound access to it that the scraper itself can use. In S5
  every scrape from the audit container was reset by the outbound relay, and
  Run failed after about 36 s with "The run produced no output rows", while
  a hand-built `curl` to the same address returned 200. A TLS-inspecting
  corporate proxy is likely to do the same. A `curl` check is not evidence;
  only a real tutorial Run on the demo host is.
- **Concurrency (hands-on only; spike S5).** One instance runs 4 composer
  turns at once (`composer_async_worker_concurrency`, default 4, max 16).
  Queued turns spend their own 240 s deadline, which starts at submission,
  and the 65th waiting turn is refused with "The composer is busy".
  - **Measured live (S5 stage 2, one instance, simple build turns, 96
    turns):** every turn completed at N = 1, 5, 10 and 20. On defaults,
    completion p95 was 62 s at 5 users, 117 s at 10 and up to 89 s at 20 (the
    wait is queueing behind 4 workers). With 16 workers, N = 20 gave p95
    40–65 s and max 62–69 s, CPU about 50% average. Provider speed varied
    3.6–10.8 s per call between runs, so treat each run as indicative.
  - **Set for hands-on use:** `ELSPETH_WEB__COMPOSER_ASYNC_WORKER_CONCURRENCY=16`,
    `ELSPETH_WEB__COMPOSER_ASYNC_MAX_QUEUED_OPERATIONS=128` and
    `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=420`. Run hands-on in waves of
    **20 or fewer** simultaneous senders (5 or fewer if the defaults stay).
    Do not have 80 people press Send at once on one instance: that was only
    modelled (on defaults: 44 completions, 20 timeouts, 16 "busy").
  - Local-auth logins are limited per client IP
    (`ELSPETH_WEB__AUTH_RATE_LIMIT_PER_MINUTE`, default 20). Raise it, and
    set the trusted-proxy hosts if a whole room shares one network.
  - Pipeline runs execute one at a time per process: do not have attendees
    press Run together.
- **Concurrency versus the event loop.** Composer turns share the serving
  event loop, and heavy CPU work (deepcopy, canonical JSON, redaction) runs on
  it (plan of record S-3; hot-path recon). Raising concurrency from 4 to 16
  admits more turns but also makes every user's stream and HTTP calls stall
  more. Measure p90 at the chosen setting during the preflight; do not assume
  16 is safe.
- **Provider credit.** When the provider refuses for credit, every turn fails
  with "The composer model is unavailable", while `/api/system/status` still
  reports the composer available (S5). Pre-fund the account and check the
  balance in preflight.
- **Timeout.** Consider raising `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` (P6).
  Planner turns ran p90 62 s, max 242 s.

### Preflight

0. A 5-token completion on the composer model and on the advisor model
   succeeds through the configured provider, and the account balance covers
   the day.
1. On the demo host, with the demo model pair and the guard in `required`
   mode, run `evals/composer-live-audit` scenarios s01, s02, s03, s04, s05,
   s20 and r01 ×3 (s04 carries "What can you do?" and "Build me a
   pipeline.").
2. Run `turn_table.py` on the results. **Pass** = no FAILED, WITHHELD or
   GATE-NOT-CLEARED labels on any prompt you intend to use.
3. Walk the tutorial in a browser through the **run** stage, including the
   scrape. **Pass** = Run returns result rows (3 in S5's script), not "The
   run produced no output rows".
4. Check the provider account balance covers the day (S5).
5. Pre-warm one throwaway session per demo flow.
6. If attendees go hands-on: a load rehearsal on the demo host at the
   expected wave size (N = 20, twice), with the guard in `required` mode.
   **Pass** = no "busy" refusals, no deadline expiry, p95 under 90 s.

### Prompts

- **Safe in every run** (with this model pair; only the first prompt has
  repeats, the others ran once, so preflight is what proves them):
  - "Here's our feedback export … classify complaint / compliment / question
    … one-line summary … CSV", with one attached CSV;
  - "Change the threshold to 500";
  - "Explain what this pipeline does in two sentences";
  - "Undo that last change";
  - "What can you do?";
  - "Build me a pipeline." (it asks good questions).
- **Usable with care:**
  - "Also put the complaints in their own separate file" worked, but took
    110 s.
  - "Why did you choose that design?" can end with a "completion advisory
    review did not clear" note. The answer still shows, and Run still works.
- **Avoid until fixed:**
  - two or more files attached to an empty session (F1). Even when the
    composer explains the limit honestly, the advisor withholds that reply
    on every model tested (S2, S4);
  - "Can you connect to X?" / "Can you read PDFs?" / "Is this safe on
    sensitive data?" (F2; SharePoint withheld 2 of 2);
  - "What changed since…?" (F3);
  - prompts that ask to screen for passwords or secrets (F4);
  - "Use GPT-4o" / "Which models can I use?" (F5);
  - the many-rows → one-briefing shape unless R11 lands (F11, a HIGH warning
    on screen).
- **Audience capability questions:** answer them verbally; do not type them
  into the composer.
- **Expect review cards** on every first build of made-up data. By design,
  the tutorial needs 5 learner decisions.

### Tutorial

- Until R16 lands, the tutorial is only as fixed as the presenter keeps it.
  After **Send tutorial brief**, do not type in the chat box, and do not
  press **Apply**, **Repair**, **Ask the composer about this**, **Edit the
  list** or **Use as input**. Only Accept the proposal, Acknowledge the
  cards, use View prompt then Approve on the prompt card, then Continue to
  Run.
- If Continue to Run refuses ("does not match the supported tutorial plugin
  set"), the request was changed. Exit the tutorial and start it again. Do
  not repair it by typing, which defeats the fixed script.
- If attendees do the tutorial **hands-on**, any of those controls can break
  it: land R16 first, or brief them explicitly.

### On a completion note

If a valid pipeline shows "Completion advisory review did not clear", the
Run button stays enabled (verified in a browser; execution itself was not
exercised in this audit). Do not ask the composer about the gate on stage
(F16, F17).
