# Prior remediation attempt: true state on the current base

Recon, read-only. Base checked: `integration/composer-remediation` at `8f5ca4292`
("Merge 0.8.12 tip into the 0.8.13 candidate"), `pyproject.toml` version `0.8.13`.
Written 2026-11-13.

Evidence grades used below:

- **[C]** verified in code or git on the current base (command run, output read).
- **[D]** read from a document only (changelog, report, plan).
- **[I]** inferred; not checked.

## 0. Things the stitched plan must know first

1. **Three backlogs exist, not two.** `CHECKLIST.md` is the original 2026-11-09 tool
   audit tracker: 320 items in 16 sections, 5 ticked, 315 open, plus 38 decisions of
   which 2 are decided [C: `grep -c`]. `NEXT-SESSION.md` (A1–A3, B1–B19) is a later,
   much smaller list written after 0.8.12 shipped. Neither it nor the CDA plan
   covers most of the checklist. The checklist was never updated after 0.8.12
   landed, so its boxes understate what is done (`reports/loose-ends-sweep.md` §6).
2. **"D1" and "D2" are decisions, not phases.** In the prior attempt D1 = "refusals
   and no-ops go back to the model; stage only what passes validation-only
   admission", D2 = "keep file-backed MCP source authoring; reject out-of-root
   paths at admission" (CHECKLIST.md "Decisions"). The work done under them is
   named `d1-*` and `d2-*` in `reports/`. Both are fully on the current base (§5).
3. **Identifier collisions.** The CDA plan reuses `D1`–`D9` for different
   decisions, `R1`–`R17` for fixes (the checklist also cites audit ids `R1`, `R4`),
   and `F8` for a finding (the prior ruling's "F8, EV-227" is a security-assurance
   pack control). A stitched plan needs prefixes.
4. **The CDA plan was written against 0.8.11 `main`.** The desk-audit branch's
   merge-base with the current base is `1a7ff0e63` (the 0.8.11 release merge);
   its prototype patches "apply cleanly at `58e31760`", which is on that branch
   [C: `git merge-base`]. It has zero mentions of `file_inject`, the context
   budget, `approval_batch_not_atomic`, pagination, batch plugins, MCP in the
   lane briefs, or Python 3.14 [C: grep counts over the CDA directory]. Every CDA
   line number and several premises predate 0.8.12 and 0.8.13 (§3.3).
5. **0.8.12 is not on `main`.** `origin/main` is `1a7ff0e63` (0.8.11) on the
   remote as of this recon [C: `git ls-remote`]. `origin/fix/composer-remediation-0.8.12`
   (`43e5f66cb`) is an ancestor of the current base.

## 1. Status of each NEXT-SESSION.md item on the current base

### A. Design work

| Item | Status | Evidence |
|---|---|---|
| A1 per-surface skill restructure | **OPEN** | [D] `CHANGELOG.md:205-206`: "The text was not restructured; about a third of what the proposal planner receives still describes tools it does not have." No `skills/pipeline_planner.md`. Design lost (§4). |
| A2 one cumulative proposal | **OPEN** | [D] `CHANGELOG.md:178-179`: "This contains the defect and does not redesign it: auto-commit sessions and blob-only proposals are not covered." [C] `approval_batch_not_atomic` registered and unexplained (§1 B12 probe). Design lost. |
| A3 opted-out session loses the turn (409 `stale_compose_state`) | **OPEN** | [D] No changelog entry. Mechanism documented in `reports/astra-followup-defect-research.md` §B. Design lost. Not re-reproduced here. |

### B. Small defects

| # | Status | Deciding evidence |
|---|---|---|
| B1 stringified `pipeline` | **PARTLY** (contained; ruling not implemented) | [C] `pipeline_planner.py:2861-2872` `_terminal_payload_shape_violations` returns a located violation, `"pipeline must be a JSON object, not a JSON-encoded string."`; sole caller `:4881` feeds it to `_canonical_schema_feedback`. Nothing decodes the string. The 2026-11-10 ruling was "decode at the provider boundary". |
| B2 `diff_pipeline` baseline | **PARTLY** (answers; baseline is request start; ruling not implemented) | [C] `tool_batch.py:2254` and `:3248` pass `baseline=ctx.turn_evidence.effects.initial_state`; `:3292` comment "The diff baseline is request-start state". No `baseline_kind` / `last_accepted` in `tools/generation.py`. [D] `CHANGELOG.md:186-189`: "the intended baseline is still to be decided". |
| B3 canned "review cards" reply to a first-turn file question | **UNKNOWN** | [C] the fixed staged-review messages still exist (`composer/protocol.py:229-244`). Not reproduced; needs a live or scripted first turn. |
| B4 preview returns no rows | **UNKNOWN** | Not checked beyond locating `_execute_preview_pipeline` (`tools/generation.py:4677`). Needs a run. |
| B5 blob listing pagination | **OPEN** | [C] no cursor/page token in `web/blobs/*.py`; the planned HKDF info `elspeth-blob-inventory-cursor` appears nowhere in `src/`. Reviewed plan survives (§4). |
| B6 `get_plugin_assistance` takes `plugin_name` | **OPEN** | [C] `tools/generation.py:2431-2444`: property `plugin_name`, `"required": ["plugin_type", "plugin_name"]`. |
| B7 no boot on CPython 3.14 (`os.pidfd_open`) | **OPEN** on the base and on the 0.8.14 candidate | [C] `web/process_watchdog.py:91` `self._pidfd = os.pidfd_open(self._target.pid, 0)`, unconditional; `pyproject.toml:6` `requires-python = ">=3.12"`. `origin/claude/zen-turing-pphown` has the same line 91 and no commit mentioning pidfd; that branch makes 3.14 the only supported interpreter, so B7 becomes blocking there if the original trace still reproduces. Not re-run. |
| B8 `remove_output` edge removal never exercised live | **UNKNOWN** (live-only) | [D] `CHANGELOG.md:190-192` records the fix. No live evidence in `reports/`. |
| B9a `explain_validation_error` lacks `canonical_schema` (+5) | **OPEN** | [C] live probe: `explain_validation_code("canonical_schema")` is `None` while the code is in `REGISTERED_ERROR_CODES`. |
| B9b `_validate_transform_provider_config_path` ordering | **UNKNOWN** (prior note says "may be a no-op") | Not checked. |
| B9c contradictory `invented_source` row refused on a verbatim bind | **OPEN** [I] | Only teaching text found (`composition_completion.py:299-302`, `:459`); no refusal located. `reports/skill-rework-report.md` §11.6 item 12 describes the defect. |
| B10 credential-shaped reasoning loses the turn | **OPEN** | [C] `web/credential_guard.py:91-128`: `require_no_credential_material_in_llm_metadata` still passes `reasoning_content` and `reasoning_details` to `require_no_credential_material`, which raises `CredentialMaterialRefused`; thinking blocks go through `native_thinking_blocks`. No withhold path, no stored withhold record. |
| B11 `batch_replicate` `copy_index` repair text; `batch_stats` output fields | **OPEN** [I for the repair text] | [C] `batch_replicate.py:60-93` declares `copy_index` as a node-created field in plugin semantics; no composer-side text naming it (`grep copy_index src/elspeth/web/composer` returns nothing). The generic `schema_contract_violation` guidance at `tools/generation.py:1680` is unchanged in kind. Not re-run live. |
| B12 rejection codes without an explanation | **OPEN** | [C] live registry, this base: 269 registered, 169 with `explain_validation_code(...)`, 100 without (controls: `schema_contract_violation` explained, a made-up code not registered). The prior count was 268/174/94 by a different instrument (lost recon); treat the two as "about 100", not as a regression. No model-facing split in `error_codes.py`. |
| B13 `gate_expression_preview_memory_exhaustion` unreachable | **PARTLY** | [C] core half open: `core/expression_parser.py:1338-1339` `except Exception as exc: raise self._unexpected_error(exc, kind="unexpected_error")` still swallows `MemoryError`. Skill half done: `pipeline_composer.md:1491` now reads "Apply the diagnostic's own `suggested_repair`…" and `:1493` gives the memory code its own row. |
| B14 `fork_coalesce` +57% reasoning | **UNKNOWN** (never investigated) | `reports/deepseek-final-battery-0812.md` only. |
| B15 gate preview samples a cut CSV row | **DONE** | [C] merged by `99bd0a7e1` (commits `65d6fb508`, `03a709a63`, `881cbc10f`). `tools/generation.py:3010-3031`: "Bounded rows from the head of an observed CSV, never including a cut record", and the `if cut and record_end == len(text) and not _csv_record_is_terminated(...)` break. `CHANGELOG.md:195-201`. Residual, stated there: preview keys rows by raw CSV header, runtime uses normalised names. **The "lost" branch `fix/composer-gate-eval-kind-repair` is therefore not lost work.** |
| B16 "backend-owned / never surface" wording | **MOSTLY DONE** | [C] `pipeline_composer.md:51` "**The server raises every prompt review card; do not raise one yourself.**"; `:59` and `:1397` forbid the old words in user prose. Residue: the internal term "backend-owned" still appears at `:1185-1186`, `:1336`, `:1393`, `:1396`. Whether the register rule holds live was not re-measured. |
| B17 tutorial Apply adds a sink, launch refused | **OPEN** | [C] `composer/tutorial_service.py:174` `output_valid = len(output_ids) == 1 and output_ids[0] == PluginId("sink", "json")`; `:195` returns the generic "does not match the supported tutorial plugin set". |
| B18 "The model's final reply is unavailable" stays in the transcript | **OPEN / undiagnosed** | [C] string at `composer/no_tool_policy.py:112`. 0.8.13 added a different, explained notice for the context-budget case only (`CHANGELOG.md:86-91`). |
| B19a no Retry after failed compose + reload | **OPEN** | [C] `TutorialFreeformShell.tsx:73` still returns "…Ask it to continue in the chat." |
| B19b pending account told "Invalid username or password" | **UNKNOWN** | Not located by grep in `web/auth`; needs a run. |
| B19c audit step shows start time in UTC as if local | **UNKNOWN** | [C] `TutorialTurn5AuditStory.tsx:103` uses `new Date(summary.started_at).toLocaleString()`, which is correct only if `started_at` carries an offset. Not checked at the API. |
| B19d "LLM calls 4" unexplained | **DONE** | [C] `TutorialTurn5AuditStory.tsx:90` "This includes one reachability check: before a run starts,…"; `CHANGELOG.md:237-238`. |

### C. Operator decisions outstanding (as listed in the note)

| Item | Status |
|---|---|
| AWS ECS default allowlist carries no batch plugins | **OPEN** [C]: `deploy/aws-ecs/terraform/modules/scenario/locals.tf:170` and `variables.tf:463` list `transform:file_inject`; no `batch_*` entry anywhere under `deploy/`. |
| 30 trust-tier findings await the judge pass | **OPEN** [D] (`reports/tier-adjudication-0812.md`). Not re-measured. |
| `a1cc53b6d` must not ship | **Not shipped; branch still exists.** [C] it is not an ancestor of the base; `origin/fix/composer-tool-remediation` still resolves to `a1cc53b6d` on the remote. |

### D. Housekeeping

| Item | Status |
|---|---|
| Recalibrate thresholds (scaffold allowance 3,878 vs approved 3,494) | **DONE as a recorded ruling** [C]: `tests/unit/web/composer/test_pipeline_planner.py:299-304` "The value follows the measurement under the operator's standing direction that this threshold tracks the working system", `_SKILL_REWORK_0812_CANONICAL_BYTES = 3878`, followed by a second authorised `file_inject` increment. |
| `test_no_mutation_alias_wrapper_…` 582 s in a worktree vs 5 s in an archive | **UNKNOWN / unexplained** (not re-measured). |
| Two `test_portable_supervisor.py` tests fail under `-n 4` | **DONE by quarantine** [C]: both carry `@pytest.mark.timing_sensitive`; the default selection excludes the marker (`pyproject.toml:487`). |
| Alias-gate red that did not reproduce | **Unchanged** (investigate only on recurrence). |

## 2. Operator rulings of 2026-11-10

| Ruling | Implemented on the base? |
|---|---|
| B10: withhold reasoning, keep the turn, cover Anthropic thinking blocks, store a record of each withhold, update the assurance pack (F8, EV-227) | **No** [C] (§1 B10). |
| General: every refusal leaves the model a terminal "return to the user with the problem" move; check it when reviewing A1–A3 | **No systematic check exists** [I]. `handoff_to_user` exists as a planner recovery tool (`capability_skill.py:40`); no test asserts the property per refusal. |
| B1: decode stringified `pipeline` at the provider boundary | **No** [C]; contained with a located rejection instead. |
| B2: baseline = last accepted state; define "accepted" with A2 | **No** [C]. |
| B17: the tutorial brief asks for the failed-rows output; `_tutorial_launch_blocker` admits it in the same change | **No** [C] (`tutorial_service.py:174`). |
| B19: explain the preflight call in the audit-step copy | **Yes** [C]. |
| Batch plugins to general availability; fix B11 in the same patch | **No** [C]: `web/plugin_policy/compiler.py:17-43` `REQUIRED_WEB_PLUGIN_IDS` has no `batch_stats`, `batch_replicate` or `batch_experiment_compare`. |
| Fold `file_inject` in as generally available | **Yes** [C]: `PluginId("transform", "file_inject")` in the same set; `origin/claude/happy-cray-mbfmm1` is an ancestor of the base. |
| Delete `origin/fix/composer-tool-remediation` once 0.8.12 is on `main` | **No**, and its precondition is unmet (0.8.12 is not on `main`). Do not delete before deciding §6. |
| Context budget lands as 0.8.13 with an upgrade note | **Yes** [C]: `origin/claude/lucid-lamport-wank6h` is an ancestor; `CHANGELOG.md:16-28` carries the note for the required `ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS`. |
| 0.8.11 and 0.8.12 pre-approved through to `main` | **0.8.11 yes, 0.8.12 no** [C]. |
| CI solo shard for timing-sensitive tests; apply the marker to five named ids; add a gate stage | **Yes** [C]: marker registered (`pyproject.toml:510`), CI job (`.github/workflows/ci.yaml:689-701`), gate stage `timing` (`scripts/full-suite-gate.sh:30`, `:241`). All five named tests carry the marker (`test_stop_before_hello…` at the decorator above its parametrize block). "Profile the rest of the two files" was partly done: commit `df44b4d5b` marked twelve more. |

## 3. Mapping to the CDA plan

### 3.1 Item by item

| Prior item | CDA lane | Relation | Agreement |
|---|---|---|---|
| A1 per-surface skill | CDA-12 | **Same goal** | Agrees in direction; CDA-12 is better evidenced (spike S2: a short overlay regressed rejections 3 → 8). CDA-12 omits two A1 parts: stating the approval mode in the request, and deciding whether MCP gets skill text. A1's measure (DeepSeek battery, reasoning tokens and rejections by code) differs from CDA-12's acceptance (palette-confusion rate, prompt tokens, rejections on five scenarios). `reports/guidance-parity-design.md` is a third, earlier design of the same planner overlay. |
| A2 cumulative proposal | none | **Not covered** | CDA-02 and CDA-23 touch its definitions without knowing it (§3.2). |
| A3 opted-out 409 | none | **Not covered** | — |
| B1 | CDA-10 (overlap) | CDA-10 makes `canonical_schema` rejections name the key | **Contradicts in approach**: CDA improves the rejection; the ruling removes the failure by decoding. Compatible if both land; CDA alone does not satisfy the ruling. |
| B2 | CDA-02 | **Same** | Partial conflict (§3.2). |
| B3 | CDA-14 (overlap) | CDA-14 shows the proposal description under the fixed first-turn sentence | Different defect (a question answered with a canned build reply). Not covered. |
| B4, B5, B7, B8, B11, B13, B14 | none | **Not covered** | — |
| B6 | CDA-10 | Same tool | CDA-10 decides: describe `plugin_name` explicitly, "no alias". B6 had no ruling; this settles it by documentation, not rename. |
| B9a, B12 | CDA-10 (R12 legibility) | Overlap | CDA-10 does not add explanations or split the registry. Not covered. Checklist D25 is the same question. |
| B9c | none | Not covered | — |
| B10 | CDA-18 (+ CDA-06 copy, CDA-19 pattern) | **Same** | **Contradiction** (§3.2). |
| B15 | none | Done | CDA has no lane; nothing to do. |
| B16 | CDA-12 (R15 reply register) | Overlap | Agrees. |
| B17 | CDA-01 | **Same defect** | **Contradiction** (§3.2). |
| B18 | CDA-08 (same file, `no_tool_policy.py`) | Overlap | CDA-08 fixes the withheld notice and receipt duplication, not this notice. Not covered. |
| B19a–c | CDA-01 (F20 run-state copy) | Adjacent | Not covered. |
| Batch plugins GA | CDA-03 (docs list) | **Collision** | CDA-03 tells the lane to "list the 14 always-authorized plugins from `plugin_policy/compiler.py:17-40`". The base already has 15 (`file_inject`); the ruling adds three more. The lane must read the set, not the number. |
| Scaffold byte budget (D) | CDA-13 | Same pin | CDA-13 says the budget "is exceeded by 911 bytes; re-baseline it". That figure predates the 0.8.12 and `file_inject` increments already recorded at `test_pipeline_planner.py:304-310`; re-measure. |
| Checklist D7 (freeform discovery limits) | CDA-22 (its D8) | Same | Agrees; CDA decides it. |
| Checklist D8 (planner diff baseline) | CDA-02 | Same | CDA-02 covers the web tool loop only. |
| Checklist D38 (copy for whole-batch credential refusals) | CDA-06 | Same | Agrees. |

### 3.2 Contradictions, from both texts

**(1) B10 vs CDA-18, Anthropic thinking blocks. Confirmed.**

- Prior ruling (`NEXT-SESSION.md:100-110`): "withhold the reasoning and keep the
  turn, **covering Anthropic thinking blocks**, with a stored record of each
  withhold and the assurance pack (F8, EV-227) updated."
- CDA decision D1 (`work-packages.md:59-63`): a hit only in
  `reasoning_content`/`reasoning_details` "drops that reasoning from the call
  record, records the event, and continues. **A hit in replayed
  `thinking_blocks`, content or tool arguments still fails.**" CDA-18 repeats it
  (`:753-754`).
- Where they agree: a stored record. CDA-18: "The event is recorded with
  surface and detector category, never the value." So "stored record" is not the
  contradiction; thinking blocks are.
- The prior review's finding was exactly that a withhold "does not help Anthropic
  (signed thinking blocks still refuse)" (`NEXT-SESSION.md:85`). CDA-18 as
  written reproduces the fix that review judged incomplete.
- Missing from CDA-18 altogether: the assurance pack update, and the "way out"
  requirement.
- New in CDA-18, absent from the prior work: never pattern-scan opaque fields
  (`reasoning.encrypted`, signatures). That is additive and compatible.
- CDA-19 (narrow `credential_assignment`) is compatible: it keeps
  `"password": "Tr0ub4dor&3"` and "the s06 reasoning sample" refusing, so B10's
  trigger remains a hit that the withhold must handle.

**(2) B17 vs CDA-01, the tutorial's failed-rows output. Confirmed.**

- Prior ruling (`NEXT-SESSION.md:115-117`): "the tutorial brief asks the Composer
  to create the failed-rows output as part of the pipeline.
  `_tutorial_launch_blocker` must admit that output in the same change."
- CDA-01 (`work-packages.md:216-248`): hide suggestion Apply (and all free input)
  in a frontend script mode; make the 409 detail name the extra output;
  "**Out of scope.** Relaxing the one-output admission rule." The CDA
  remediation plan says the relaxation option is "superseded: relaxing the
  one-output admission rule would weaken the canary" (`remediation-plan.md:41`).
- Both are operator rulings (2026-11-10 and 2026-11-13). They answer the same
  defect in opposite ways: one widens what the tutorial builds and admits, the
  other removes the control that led there.
- A second tension: `NEXT-SESSION.md:92` says "a tutorial-only suppression is not
  allowed". CDA-01 argues its script mode is frontend-only and so adds no
  backend tutorial path (invariant 2). That is a reading of the invariant the
  maintainer accepted on 2026-11-13; the prior note read it more strictly.
- Needs one ruling. They can also compose (brief creates the output, admission
  admits it, and the Build step is script-only), but CDA-01's out-of-scope line
  forbids that as written.

**(3) B2 vs CDA-02, what "accepted" means. New.**

- Prior ruling: "compare against the last accepted state. Settle the definition
  of 'accepted' with the A2 design."
- CDA-02 defines it alone: "the newest proposal with `status='committed'` and its
  `committed_state_id`", and keeps request-start as a second baseline with
  `baseline_kind`.
- Conflict: in auto-commit sessions, and under A2's cumulative proposal, "newest
  committed proposal" may not be the state the user last accepted. CDA-02's
  definition should be marked provisional on A2.
- CDA-02 is also partly stale: its first file change, "`tool_batch.py`: pass
  `baseline=` to the dispatch of `diff_pipeline`", is already on the base
  (`tool_batch.py:2254`, `:3248`), and so is the "says so when held proposals are
  not included" wording. What remains of CDA-02 is `baseline_kind`, the
  last-accepted source, and the `is_valid: false` correction.

**(4) "Way out" ruling vs CDA-18 and CDA-22. New, lesser.**

- The ruling: "no control may leave the model without a terminal 'return to the
  user with the problem' move."
- CDA-18 leaves three hard failures (thinking blocks, content, tool arguments)
  with no stated model-facing exit. CDA-22 adds a hard discovery cap but does
  give one last-chance no-tool call, which satisfies the ruling. CDA-17's
  deadline hand-off satisfies it. No CDA lane checks the property generally.

**(5) A2 vs CDA-23 (skip identical snapshots). New, design-level.**

- A2 redefines the working state and proposal lifecycle and wanted session epoch
  79. CDA-23 changes which writes mint a `composition_states` row and adds a
  `composition_state_unchanged` audit row. Neither text knows the other. If A2 is
  to be redone, do it before or together with CDA-23, or CDA-23's no-op rule
  will be defined against a state model A2 then replaces.

**(6) B1 vs CDA-10.** See the table: not a clash of rulings, but CDA-10 alone
leaves the approved ruling unimplemented.

### 3.3 CDA premises that predate the base (stale, verify per lane)

- CDA-02: baseline already passed (above).
- CDA-03: "14 always-authorized plugins" is now 15.
- CDA-13: byte-budget excess figure.
- CDA-12: the planner prompt-size targets (45k tokens) predate the 0.8.13
  context budget, which sheds context and changed the default request-byte
  ceiling to 960 KiB (`CHANGELOG.md:24-28`).
- CDA-10/CDA-18: `canonical_schema` and credential paths were edited by 0.8.12;
  every cited line number needs re-anchoring.
- CDA-01: cites `tutorial_service.py:169, :195`; those lines still match on the
  base [C].

### 3.4 Prior items no CDA lane covers

A2, A3, B1 (the decode), B3, B4, B5, B7, B8, B9a–c, B11 and batch-plugin GA, B12,
B13, B14, B18, B19a–c, the ECS allowlist decision, the 30 trust-tier findings,
and almost all of `CHECKLIST.md` sections 3–16.

## 4. What survives of the lost work

Four branches are gone from the remote [C: `git ls-remote` returns only
`fix/composer-tool-remediation`]. No commit on any surviving ref carries their
content (`git log --all --grep` for the design and withhold subjects finds only
unrelated commits). There is no `design/` directory on the base.

| Lost | What survives in `reports/` | Irrecoverable |
|---|---|---|
| **A1 design** | `skill-prompt-audit.md` (who receives which text, contradictions, wrong-surface guidance per surface, ranked cut list, the pins that bind skill text: §0–§7). `skill-rework-report.md` §7 "Plan for the deliberate pass" (per-surface files, assembly and migration cost, remaining cut list) and §11.6 item 13 (approval mode not stated). `deepseek-planner-trace-findings.md` (confusion points C1–C8 with character counts). `guidance-parity-design.md` + review + implementation brief: a complete, reviewed, bounded planner-overlay design from the earlier phase. | The A1 design text and its review; the list of **instrument defects** (planner byte ceiling, battery driver's skill hash) that `NEXT-SESSION.md:186-187` says apply to any before/after claim. Rediscover those before trusting a battery comparison. |
| **A2 design** | `astra-followup-defect-research.md` §A (root cause, four options compared, recommendation: one cumulative proposal; states it is "substantially more than replacing the loop's `state` variable") and §E (auto-commit first-turn proposals left pending). `approval-containment-report.md` (what 0.8.12 built, the four decisions taken, and "Scoped follow-up": seven things still broken). `approval-containment-review.md`. `deepseek-postmerge-probe.md`. | The cumulative-proposal design, its step plan, its epoch-79 schema change, its session-context object shape. |
| **A3 design** | `astra-followup-defect-research.md` §B: verified caller chain with file:line, the stale-head mechanism, three options, recommended direction ("advance expected head, carrying the exact produced record"), PostgreSQL implications and proposed tests. This is close to a design. `deepseek-followup-tool-probe.md`, `quick-wins-triage.md` QW1. | The written design, its `head_advanced` contract, its epoch claim. |
| **B9 fixes** (`fix/composer-0812-evening`) | `skill-rework-report.md` §11.1 and §11.6 items 11–12 state the two defects. | The three WIP commits and `evening-fixes-0812-report.md`. Small; rewrite test-first. |
| **B12 recon** (same branch) | Nothing. | `rejection-code-explanation-recon.md` (the 94-code classification, 15 audit categories, 3 dead codes). Re-derive from the live registry; the probe in §1 B12 takes seconds. |
| **B10 fix** (`fix/composer-reasoning-credential-withhold`) | Only the summary at `NEXT-SESSION.md:85` and the ruling. | The patch, its report and the adversarial review. CDA-18 is a fresh brief for the same change and names every call site. |
| **B15 fix** (`fix/composer-gate-eval-kind-repair`) | **Landed** (`99bd0a7e1`), with `gate-eval-kind-repair-report.md`, `-review.md`, `gate-repair-0812-*.md`. | Nothing. |

**Seams the three designs contradicted each other on** (`NEXT-SESSION.md:17-29`;
the reconciliation was never done, so a redo must settle these up front):

1. A2 and A3 both claimed session epoch 79.
2. Stale-proposal retirement: A3 at write time (`head_advanced`); A2 at compose
   start (`base_conflict`), and A2 refuses `request_interpretation_review` while
   a proposal is pending, which makes part of A3's contract moot.
3. A1's first step and metric are built on `approval_batch_not_atomic` and
   `approval_proposal_pending`, which A2 deletes; A1 and A2 give the session
   context object different shapes.
4. A2's "safe stopping point" after step 3 is unsafe in explicit-approval mode
   (a planner supersede can silently replace the user's pending plan).
5. The `diff_pipeline` baseline (A3) is not reconciled with A2's working state.

## 5. CHECKLIST.md and RESUME.md

**Structure.** `CHECKLIST.md` is a flat tracker, not phased: 16 topic sections
(baseline; invocation context and approval lifecycle; identity and pagination;
blob reads; blob mutation; plugin catalogs; secrets; graph validity; source and
output editing; interpretation reviews; no-ops and audit; preview and repair
guidance; planner guidance; advisor and recovery; user-visible text; shared
contracts), then decisions D1–D38, then an activity log. Its stated execution
order: "Baseline reconciliation → approval/context failures and
identity/retention defects → crash and integrity handling → graph/review
consistency → progress/replay contracts → remaining discovery, guidance and
presentation work."

`RESUME.md` is the 2026-11-09 pause note at checkpoint `dd3b3524c`. Its work
table lists eleven integrated slices under D1 and D2.

**Are they on the base?** Yes, all of them [C]. `git cherry HEAD
origin/fix/composer-tool-remediation` marks 18 of 21 commits patch-equivalent.
The other three:

- `e192aa135` (fixture alignment): present as `1df82e05d` (same subject, patch
  changed in rebase).
- `1bc91710f` (MCP path admission): present as `1872ed805`.
- `a1cc53b6d` (lane assets): deliberately absent.

Base equivalents of the lifecycle slices: `8a9c46bb8` (lifecycle foundations),
`919c3bf7a` (portable supervisor), `e077d129a` (public standalone lifecycle),
`5a9fd13d1` (queue scope). `CHANGELOG.md:265-274` describes them.

**Is "D2" lifecycle work only on `origin/claude/zen-turing-pphown`? No.** That
branch is not an ancestor of the base (374 ahead, 58 behind), but its only
change under `src/elspeth/composer_mcp` and `web/process_watchdog.py` relative
to the merge-base is two lines in `composer_mcp/runner.py` from
`08be83b2d fix(logging): keep exception content out of asyncio's diagnostics`
[C: `git diff --stat HEAD...`, `git log HEAD..`]. It is the Python 3.14 release
(0.8.14): 3.14-only, `JSON_LOGS=true` refused, plugin strictness changes. Its 29
commits touching `web/composer` are rebased copies of the 0.8.13 context-budget
commits plus two 3.14 fixes. It carries no remediation work the base lacks.
Stitching consequence: the composer plan will eventually rebase across a
3.14-only tree, and B7 must be settled there.

**What RESUME.md said was next, and what happened:**

| RESUME "pending" | Outcome |
|---|---|
| Lint-delta adjudication | Done as far as agents can: `lint-set-diff-0812.md`, `tier-adjudication-0812.md`; 30 findings await the operator's judge pass. |
| Broad Astra review of the branch | `astra-combined-review.md`, `loose-ends-sweep.md`. |
| Combined ordinary, proof and PostgreSQL gates | Run during the 0.8.12 day: `six-reds-0812-report.md`, `gate-reds-0812-report.md`, `fencing-failures-0812-report.md`, `gate-repair-0812-*.md`. Final verification status "is recorded in the pull request" (`CHANGELOG.md:143-144`), not in the tree. Not re-verified here. |
| Blob pagination (five-task plan, task 1 preflighted) | **Not started.** = B5. |
| Recovery-audit classification, tracker 11.19 | **Done** [C]: `pipeline_planner.py:1307` unions `PLANNER_RECOVERY_TOOL_NAMES` into the known set; `CHANGELOG.md:214`. |
| Guidance parity (planner overlay) | **Not started.** = A1 / CDA-12. |

After RESUME came the 0.8.12 quick-wins day (2026-11-10) and `NEXT-SESSION.md`.
What was next at the end: reconcile A1–A3, then implement; B1–B19; the rulings.
None of that has started on the base beyond B15, B16, B19d, `file_inject`, the
context budget and the timing shard.

**Checklist drift to correct before using it** (`loose-ends-sweep.md` §6):
09.01/09.02 are fixed but unticked; 02.07–02.12 say "in progress" though their
commits landed; 03.01–03.05, 02.17, 02.18, 11.19 are integrated but unticked.
Both files cite `/workspace/elspeth` paths and an external ChatGPT "live
checklist Page" as the authority; neither exists for the next run.

## 6. The saved lane evidence in `a1cc53b6d`

11,688 files, almost all logs, pytest captures, mutant snapshots, Playwright
browsers and runtime binaries (largest: `park-identity-evidence` 3,280 files,
`d1-stage-race-baseline` 1,966, `d2-checks` 1,549, `playwright-browsers` 599).

**No reviewed diff for unmerged work is in it.** Every `.diff`/`.patch` (55
files) belongs to a slice that landed: `d1-*`, `d2-*`, `queue-*`,
`park-identity-*`, `cache-audit-*`, `task-1..3`, `envelope-splat`,
`combined-wave-review.patch`. The lane's own `progress.md` confirms pagination
and recovery-audit reached brief/preflight only. I did not run `git apply
--check`; the `git cherry` result above already shows the commits these diffs
produced are on the base.

Documents present there and **not** in `reports/` that are worth a `git show`
before the branch is deleted:

| File (under `.claude/lanes/composer-remediation-wave1/`) | Why |
|---|---|
| `pagination-design.md`, `pagination-implementation-brief.md` | Earlier companions to the surviving `blob-pagination-plan.md`; useful if B5 is scheduled. |
| `guidance-parity-design-fix1.diff`, `guidance-parity-design-fix1-notes.md` | The corrections applied after the design review; shows what changed between the two design versions for the A1/CDA-12 overlay. |
| `d2-mcp-path-map.md`, `d2-platform-lifecycle-map.md` | Route maps for the MCP surface, which the CDA plan does not look at. |
| `recon.md`, `d1-recon.md` | The 01.01 tool-surface measurement (41/43/45/23/46/32) cited by the checklist. |
| `runtime-deps/pytest_plugins/composer_test_cache.py` | The Chroma cache plugin the RESUME runbook requires for ordinary and proof collections. |
| `compare-lint-corpora.py`, `d2-checks/freeze_tree.py` | The lint-corpus comparer and tree-freeze helper. `scripts/full-suite-gate.sh` now covers freezing; the comparer has no tracked successor that I found [I]. |
| `combined-foundations-lints.json` | The 5,433-finding qualified baseline. Superseded for practical purposes by `lint-set-diff-0812.md`. |

Recommendation: extract those few files to a tracked or lane path, then the
approved deletion can proceed once 0.8.12 is on `main`. The lane `.gitignore`
there notes that some files were excluded for credential-shaped strings, so the
commit is not a complete record either.

## 7. Working practice to carry forward

From `NEXT-SESSION.md` "How to work it", `RESUME.md` and the reports:

- **Environment.** A worktree with **its own venv**: `uv sync --frozen
  --all-extras --python 3.13`. A symlinked venv reports the main checkout's
  version and fails the DAG scenario goldens. `npm ci` at the root and in
  `src/elspeth/web/frontend`. (This worktree has its own `.venv` directory [C].)
- **Provenance.** Export both `<tree>/src` and `<tree>/elspeth-lints/src`, pass
  the matching `-o pythonpath`, and check both module `__file__` paths. The CDA
  plan independently found the same trap (its S3 note).
- **Test scope for any composer change.** Run the whole of
  `tests/unit/web/composer`, `tests/unit/web/execution`,
  `tests/unit/web/sessions` and `tests/unit/architecture`. Reason recorded: the
  2026-11-10 reviews "found four defects the authors' own test runs missed, each
  time because the author had not run the file that pinned the behaviour." CDA
  lane commands name one to four files each; that is narrower than this rule.
- **Review.** Every code change gets an independent review before or just after
  merge. Implementation by one model, review by another. Bounded briefs,
  file-backed reports. Mutation or negative controls where they establish the
  boundary. One consolidated fix wave and a scoped re-review, not repeated broad
  reviews.
- **Capacity.** One broad suite at a time. The old host (5 CPUs, 17 GiB) hit OOM
  at 12 workers and used 4; re-measure on this host. PostgreSQL is serial.
- **Gates.** `scripts/full-suite-gate.sh --execute --detach` with an explicit
  `--root`; read `summary.txt`; `frozen=NO` voids the run. The three long proofs
  in `tests/unit/architecture/test_web_landscape_mutation_fencing.py` run under
  `scripts/ci_proof_progress.py` with a 7,200 s timeout; do not interrupt one
  merely for running long. The lint corpus is compared before and after, never
  to zero; do not re-pin or suppress to go green.
- **Real dependencies.** Some suites need `sqlite3`, Caddy, Terraform plugin
  cache, Playwright browsers and a real Chroma model cache; without them the
  earlier run showed 52 Chroma failures, 18 Caddy failures and 139 Terraform
  setup errors that were environmental. 23 Terraform tests skip without the
  binary.
- **Known slow or flaky.**
  - `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists`: 582 s
    in a worktree against about 5 s in a `git archive` extract, unexplained.
  - The `timing_sensitive` set (now 17+ ids across
    `test_portable_supervisor.py`, `test_standalone_lifecycle.py` and others)
    runs only via `-m timing_sensitive -n 0` or the gate's `timing` stage;
    GitHub issue #360.
  - Reviewed full-source guard pins go stale on any edit to pinned files
    (`loose-ends-sweep.md` §1); CDA-21 names two such files.
- **Live measurement.** The isolated DeepSeek probe and reasoning tap
  (`deepseek-followup-tool-probe.md`, `deepseek-postmerge-probe.md`) never touch
  the live service and never call `/execute`. The 19-case battery baseline is in
  `deepseek-final-battery-0812.md` (reasoning tokens −42% on 16 cases, one run
  per case). The CDA plan has its own harness (`evals/composer-live-audit`); the
  two baselines are not comparable without a same-day rerun.
- **Custody.** Agents never hold the HMAC key, never hand-edit signatures.

## 8. Not verified

- No test suite was run. No live provider call. No browser walk.
- B3, B4, B8, B9b, B14, B19b, B19c and the 582 s test were not reproduced.
- B11's repair text and B9c's absence are inferred from greps that found no
  match; a silent non-match looks the same as an absence, so treat both as
  probable, not proven.
- The claim that 0.8.12's combined gates ended green rests on the pull request,
  which I did not read.
- B7 was read in code on both branches; the 3.14 boot failure was not re-run.
