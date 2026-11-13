# Composer remediation: stitched plan (2026-11-13)

This plan joins three things that were written without knowledge of each
other, and resequences the work on the code that exists today.

| Input | What it is | Where |
|---|---|---|
| **Prior attempt** | The 0.8.12 tool remediation, its forward plan (design items A1–A3, defects B1–B19, operator rulings of 2026-11-10) and its original 320-item tracker | `NEXT-SESSION.md`, `CHECKLIST.md`, `RESUME.md` and `reports/` beside this file |
| **CDA plan** | The desk audit of 2026-11-12: 23 lane briefs, CDA-01 to CDA-23 | Branch `ccr-d14093ba-glu6bw` at `b1ec0aacd`, `docs/development/composer-desk-audit-2026-11-12/work-packages.md` |
| **Base** | The union of the 0.8.12 and 0.8.13 release candidates | Branch `integration/composer-remediation`, started at `8f5ca4292` |

The CDA plan was written against `main` at `1a7ff0e63` (0.8.11). The base is
254 commits later. Four branches holding the prior attempt's next-phase work
(`docs/composer-0812-design`, `fix/composer-0812-evening`,
`fix/composer-reasoning-credential-withhold`,
`fix/composer-gate-eval-kind-repair`) were lost with the machine and are not
recoverable. The last of the four had already landed (`99bd0a7e1`).

**Evidence.** Five read-only recon reports, written 2026-11-13 against
`8f5ca4292`, are in [reports/reanchor-2026-11-13/](reports/reanchor-2026-11-13/):
`wave1.md` to `wave4.md` re-check every CDA lane, and `prior-attempt.md`
establishes the state of the prior attempt. They were made by reading code. No
suite, live provider call or browser walk was run for them. Each lists what it
did not verify.

**Review.** A peer agent reviewed this plan against the code and the three
inputs on 2026-11-13: cleared with five fixes, all applied here
([stitched-plan-review.md](reports/reanchor-2026-11-13/stitched-plan-review.md)).

## 1. Identifiers

The two plans reuse the same letters for different things. In this plan and in
every lane brief:

- `CDA-nn` is a lane. `CDA-D1` to `CDA-D9` are the CDA plan's decisions.
  `CDA-R1` to `CDA-R17` and `CDA-F1` to `CDA-F21` are its fixes and findings.
- `P-A1` to `P-A3`, `P-B1` to `P-B19` are the prior attempt's items. `P-D1` and
  `P-D2` are its two decisions (both fully landed in 0.8.12). `P-Dnn` up to
  `P-D38` are the tracker's decisions.
- `S-n` is a ruling this plan needs from the maintainer (§4).

## 2. What the re-check found

Every CDA finding still exists on the base. None is fully fixed. Three are
partly fixed by 0.8.12, and every brief needs corrections.

| Lane | Status on base | What changed for the lane |
|---|---|---|
| CDA-01 tutorial script-only | Still present | Affordances also live in `DecisionPanel.tsx` and `InlineSourceCreatedTurn.tsx`. Hiding `ChatInput` also hides Cancel. Conflicts with P-B17 (S-1). |
| CDA-02 `diff_pipeline` baseline | **Partly fixed** (`a2ad64974`, `a33cbb6d6`) | Request-start baseline, reworded refusal and description shipped. Left: `last_accepted`, `baseline_kind`, the `is_valid: false` fix. A shipped test pins the opposite of the brief's acceptance (S-2). A new `data` key must pass the closed `DiffResponse` contract. |
| CDA-03 `report_assemble` semantics | Still present | The brief's one-line (`COMPACT`) rule is unsound: the transform does not reject line breaks inside its inputs (S-3). The required plugin set is 15, not 14; the stale list is in two docs. |
| CDA-04 node-level vague-term slots | Still present | Aid text moved to `planner_authoring_aids.py:889-897`. Naming the query needs `tools/sessions.py` and `interpretation_surfacing.py`. The hash item is under-specified. |
| CDA-05 boot probe | Still present | Production anchor is now `pipeline_planner.py:4665`. Three existing tests pin the three-tool hatch shape and must change. |
| CDA-06 credential refusal copy | Still present | The fix site is `web/credential_guard.py:52-67`, not the contracts module. Use a closed set of model-output surfaces; a name glob misses `composer_provider_thinking`. |
| CDA-07 advisor capability facts | Still present | History is built in `sessions/routes/_helpers.py` and `planning_application.py`, not `pipeline_planner.py`. Carrying a withheld draft forward contradicts the `withheld_replies.py` "never re-enter" contract (S-5). Also touches `composer/service.py`. The S1 replay requests embed the pre-0.8.12 skill text and must be rebuilt. |
| CDA-08 withheld and receipt copy | Still present | Also touches `advisor_checkpoint.py`, `composition_completion.py`, `planning_application.py`, `chat.css`. |
| CDA-09 turn-end preflight | Still present | `service.py` anchor moved to `:2262`. `no_tool_finalize.py:221` is a fourth consumer. |
| CDA-10 planner notices and rejections | **Partly fixed** (`2776b8620`) | Two `canonical_schema` shapes are named. Left: notice repeat, unexpected-key naming, named-source blob rejection, both tool descriptions. Seven tests pin the notice, not six. Discovery-failure ordering is in `provider_discovery_response.py`. |
| CDA-11 `list_models` | Still present | The planner asserts on the result shape (`pipeline_planner.py:563-584`); a closed response contract and `test_model_discovery_scope.py` pin today's behaviour. |
| CDA-12 per-surface teaching | **Partly fixed, changed shape** (`e963a5b62`) | 0.8.12 added a text-only surface orientation that the lane must unwind. Overlay, per-surface hash, archive, advisor text and the parity test are all left. Every acceptance number is stale. Same goal as P-A1. |
| CDA-13 multi-file sources | Still present | The S3 prototype no longer applies whole (5 of 7 files). The proposal blob extractor hunk needs a rewrite. |
| CDA-14 proposal description | Still present | Not frontend-only: the proposal wire has no description and redaction strips metadata values (S-6). |
| CDA-15 battery corpus | Still present | The corpus test has a closed 20-name roster and needs a `set_pipeline` payload per case; question-style cases cannot pass as briefed. |
| CDA-16 advisor cache | Still present | The planner cache marker belongs in `apply_anthropic_cache_markers`, because the 0.8.13 context budget measures marked bytes. |
| CDA-17 deadline reserve | Still present | The hand-off call must go through the planner context budget and the provider-call cap. |
| CDA-18 reasoning credential scrub | Still present | Same as P-B10, whose ruling is wider (S-4). The brief contradicts itself on scanning signature fields. |
| CDA-19 label-style prose | Still present | Narrowing the pattern also weakens egress redaction through `web/validation.py:180`. |
| CDA-20 provider 429 retry | Still present | Retry loops already exist in the tool loop and the planner. The defect is only that a 429 classifies as non-retryable (`provider_errors.py:158`). Drop the new gateway helper. Now touches `composer/service.py`. |
| CDA-21 deadline from worker claim | Still present | Files byte-identical to `main`. Two unlisted pin files. |
| CDA-22 discovery budget | Still present | The last-chance call must mirror `_prepare_primary_delivery` and pass `context_budget=`. AWS ships a limit of 8, not 10. |
| CDA-23 skip identical snapshots | Still present | 0.8.12 stops only a no-op `set_pipeline` under auto-commit. Proposal settlement now refuses identical content, so acceptance 3 is false for that writer. Interacts with P-A2 (S-7). |

Prior-attempt items, on the base:

| State | Items |
|---|---|
| Done | P-B15, P-B19d, most of P-B16, `file_inject`, the context budget, the timing-sensitive shard |
| Open, covered by a CDA lane | P-A1 (CDA-12), P-B6 (CDA-10), P-B10 (CDA-18), P-B17 (CDA-01, in conflict), the first clause of P-B19a (CDA-01) |
| Open, partly covered | P-B2 (CDA-02, pending S-2: the default fixes one of three session shapes) |
| Open, ruled on 2026-11-10, no lane yet | P-B1 decode a stringified `pipeline` at the provider boundary; the three batch plugins to general availability with P-B11 |
| Open, no ruling, no lane | P-A2, P-A3, P-B5, P-B7, P-B9a, P-B9c, P-B12, P-B13 (core half), P-B18, P-B19a |
| Unknown until reproduced | P-B3, P-B4, P-B8, P-B9b, P-B14, P-B19b, P-B19c |
| Not triaged | `CHECKLIST.md`: 320 items, 5 ticked, never updated after 0.8.12 landed |

## 3. How a lane is briefed

A lane's brief is three texts, in this order of authority:

1. this plan (§4 rulings and defaults, §5 rules, the lane's row in §6);
2. the lane's section of the matching re-anchor report (anchors, pinned
   tests, corrections, files to touch);
3. the original CDA brief in `work-packages.md` §5.

Where they disagree, the higher one wins. A line reference from any of them
is a pointer: confirm it in the worktree before editing.

## 4. Rulings needed from the maintainer

Each has a default. A lane that depends on an unanswered ruling runs only if
its default is marked "safe to start"; otherwise it waits.

| # | Question | Default | Blocks |
|---|---|---|---|
| S-1 | **Tutorial extra output.** P-B17 (2026-11-10): the tutorial brief asks for the failed-rows output and the launch check admits it. CDA-01 (2026-11-13): script-only Build step; relaxing the one-output rule is out of scope. They can compose. Note that `NEXT-SESSION.md:92` says "a tutorial-only suppression is not allowed", and hiding suggestion Apply in the tutorial is one; the later CDA-R16 decision (`work-packages.md:126-127`) accepted it because it is frontend-only. ADR-031 scopes invariant 2 to the backend. | Run CDA-01 as corrected (script mode, 409 detail, copy). Leave the admission rule alone. P-B17 stays open as its own item. **Safe to start.** | P-B17 only |
| S-1a | CDA-01: with the chat input hidden, does **Cancel** stay, and does the **interrupted-request retry** stay? | Both stay. Neither sends new content: Cancel stops the build, and retry resends the scripted brief. **Safe to start.** | — |
| S-2 | **`diff_pipeline` baseline precedence.** The 2026-11-10 ruling is "last accepted state; settle 'accepted' with P-A2". 0.8.12 shipped request-start and a test that pins it. | Use the newest committed proposal's `committed_state_id` when the session has one, else request start. Report which in `baseline_kind`. Marked provisional on P-A2. The lane first checks what auto-commit sessions hold and reports it; it does not invent a third baseline. **Safe to start; the merge waits for the answer.** The default fixes only one of three session shapes (table below), and it replaces the shipped "this request's edits" view wherever a committed proposal exists. Another defensible reading is the state *before* the newest accepted proposal. | CDA-02 merge |
| S-3 | **`report_assemble` framing (CDA-D3).** The brief's `COMPACT` rule is unsound. Options: (a) `NEWLINE_FRAMED` with a title or a line-break join, `UNCONSTRAINED` otherwise; (b) make the transform reject line breaks in inputs under the compact configuration, so `COMPACT` is true. | None. CDA-D3 was already awaiting an explicit OK. | CDA-03 |
| S-4 | **Credential-shaped reasoning.** P-B10 ruling: withhold and keep the turn, covering Anthropic thinking blocks, with a stored record, the assurance pack (F8, EV-227) updated, and a way for the model to hand back to the user. CDA-D1: a hit in replayed `thinking_blocks` still fails. | None. The two rulings conflict on thinking blocks. | CDA-18, then CDA-19 |
| S-5 | **Withheld draft carry-forward (CDA-07).** The brief keeps a withheld answer in the next turn's history as a labelled draft. `withheld_replies.py` says a withheld reply never re-enters. | Split the lane. CDA-07a (capability record, rubric, repair copy, blocker detail) is **safe to start**. CDA-07b (carry-forward) waits. | CDA-07b |
| S-6 | **CDA-14.** Add a description field to the proposal wire, or show the description only after accept? | None. | CDA-14 |
| S-7 | **P-A2 and CDA-23.** P-A2 (one cumulative proposal) would change the state model that CDA-23's no-op rule is defined against. Redo the P-A2 design first, or land CDA-23 on today's model? Also CDA-D9a to D9c. | None. | CDA-23 |
| S-8 | **Batch plugins to general availability** (ruled 2026-11-10, not implemented). Still wanted, and in this effort? | Not scheduled until confirmed. | P-B11 lane |
| S-9 | **Audit documents.** The CDA documents, eval harness and spike artifacts are only on `ccr-d14093ba-glu6bw`. Merge that branch into the integration branch? | Not merged. Lanes read them from the `composer-desk-audit` worktree. | — |
| S-10 | **Release.** Which version do these lanes ship in? | Lanes do not edit `CHANGELOG.md` or upgrade notes. Each lane's `report.md` carries a proposed changelog line. | — |
| S-11 | **Environment.** Docker (the socket refuses this user) for the testcontainer suite; `OPENROUTER_API_KEY` for live acceptance. | — | CDA-13, 21, 23; live acceptance of CDA-07, 12 |

What "What changed?" answers one turn after an edit, under the S-2 default:

| Session | Baseline | Answer |
|---|---|---|
| Explicit approval, the edit was accepted | The newest committed proposal is that edit, so the baseline is the current head | Zero changes. The P-B2 symptom remains. |
| Auto-commit, no committed proposal | Request start | Zero changes. The P-B2 symptom remains. |
| An accepted pipeline proposal, then auto-commit edits | The pipeline acceptance | Every edit since, growing for the life of the session. The only shape the default fixes. |

## 5. Rules every lane carries

These add to `work-packages.md` §4, which still applies.

- **Base and environment.** Lane worktrees start from
  `integration/composer-remediation`. Every lane worktree links `.venv` to
  the **integration worktree's** environment, a real directory built from
  this tree's lockfile. The main checkout's environment has older
  dependencies, and `scripts/worktree-cleanup.sh --link-venv` links that
  wrong one. A lane never runs `uv sync` or `uv pip install`. A frontend lane
  runs `npm ci` in its own worktree first. Put `<lane>/src:<lane>/elspeth-lints/src` on `PYTHONPATH`
  **and** on `-o pythonpath=`, and check `elspeth.__file__` and
  `elspeth_lints.__file__` before trusting a result.
- **Test scope.** A lane worker runs only its lane test command and the files
  its re-anchor section names as pinned, with `-n 4` or fewer. The
  orchestrator runs the sweep, one at a time, at `verify` for each merge:
  `tests/unit/web/composer`, `tests/unit/web/execution`,
  `tests/unit/web/sessions`, `tests/unit/architecture`, the top-level
  `tests/unit/web/test_*.py`, `tests/integration/web/composer`,
  `tests/unit/composer_mcp`, `tests/unit/elspeth_lints`,
  `tests/unit/scripts/test_composer_wire_census.py`, and for CDA-04
  `tests/unit/plugins/llm` and `tests/unit/plugins/transforms/llm`. The
  failure set is compared with a base run of the same selection
  (`.claude/lanes/baseline/` in the integration worktree). The
  `timing_sensitive` ids in these directories are deselected by default; the
  wave-end gate's timing stage covers them. Reason for the width: on
  2026-11-10 reviews found four defects the authors' narrower runs missed,
  and on this base the files that pin wave-1 behaviour sit outside the four
  directories that rule named.
- **Trust-tier bindings.** A worker never edits a `judge_metadata_signature`
  or `tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json` to go
  green. Drift is reported in `report.md`.
- **Commits.** Run `scripts/branch-safety-check.sh --intent commit` before
  each commit.
- **Settings fixtures.** `composer_context_max_prompt_tokens` is a required
  setting since 0.8.13; every new `WebSettings` fixture sets it.
- **Closed contracts.** A new key in a discovery tool's `data` must be added
  to its response contract in `tools/_generation_responses.py`, its wire
  fixture and its declaration snapshot, or admission fails.
- **Planner scaffold byte pin** (`test_pipeline_planner.py:304-338` and
  `:1717-1727`). It measures the request's tool declarations as well as its
  messages, so a reworded tool description moves it. Measure first; the
  headroom is unmeasured. If the pin trips, review the added text for waste.
  If there is no cut that keeps the meaning, add a new named constant for the
  lane (the file's pattern) with a dated comment giving the measured before
  and after, add it to the sum, and keep the exact-boundary and +1-byte
  controls. Do not edit another lane's constant. The second lane to merge
  re-measures on the merged tree. List the increment in `report.md`. The
  recorded ruling (2026-11-10) is that the cap is a trigger to review for
  waste, not a reason to cut.
- **Sha-pinned source guards.** Re-pin only after the reviewed change, and
  list every re-pin in `report.md`.
- **No changelog edits** (S-10).
- **Way out.** A lane that adds or changes a refusal the model can hit states
  in `report.md` how the model returns to the user from it (2026-11-10
  ruling).
- **Hands and review.** Implementation is done by a Codex worker
  (`gpt-6-astra`, medium effort) in the lane worktree. Each lane's diff is
  then reviewed independently by a Fable agent and by the orchestrator before
  `verify` and merge. The reviewer is given the diff and the brief, not the
  worker's account of it.

## 6. Resequenced waves

A wave starts from the tree that contains the earlier merges. Within a wave,
lanes run in parallel and merge one at a time, each re-verified against the
tree the previous merge produced.

### Wave 1 (start now)

| Lane | Scope on the base | Collisions inside the wave |
|---|---|---|
| CDA-01 | Corrected brief, S-1 and S-1a defaults. Adds `InlineSourceCreatedTurn.tsx`, `subscriptions.test.ts`, and the pinned copy tests. Script mode must leave a resend-the-brief control in every state after the brief in which no pipeline exists and nothing is composing (failed compose, text-only or withheld reply, reload); today the textbox is the only way out of those states. The control resends the stored brief by id and accepts no text. Correct the `TutorialFreeformShell.tsx:73` copy ("Ask it to continue in the chat") to name that control, and add a Vitest case for the state. With Apply hidden, the suggestion text is still shown. Vitest and the e2e walk are run by the orchestrator. | None |
| CDA-02 | What is left, per the S-2 default: `baseline_kind`, the last-accepted source, the `is_valid: false` correction. `baseline_kind` is an optional field of `DiffResponse`. The web tool loop reports `request_start` or `last_accepted`, and both `tool_batch.py` seams (`:2254`, `:3248`) use the same baseline. The planner route (`pipeline_planner.py:3822`) and `pipeline_commit.py:517, :593` report `request_start`; the lane edits `pipeline_planner.py` by that one keyword only. The MCP surface (`composer_mcp/server.py:412, :878`) omits the key. The `is_valid: false` correction changes the result for this code only, not the shared rejection helpers in `tools/_common.py`; if that is not possible, report it as not done. New cases go beside the existing request-start assertions in `test_freeform_diff_baseline.py`, which stay unchanged. **Built and reviewed in wave 1; merged only after S-2.** | `test_pipeline_planner.py` byte pin (certain if the tool description changes; shared with CDA-04), generated inventories, `skills/pipeline_composer.md:613-616` |
| CDA-04 | Corrected brief. The lane does **not** change the derivation in `approved_prompt_artifact_hash_from_options`: stored approvals are compared against it, so that is a persisted-data decision for the maintainer. It reports, from the finding's wording and the code, whether a change is needed and which stored approvals it would invalidate. `report.md` also states the effect of the new staging rejection on stored states that already carry the wiring (the brief's read-only query). Also fixes the third copy of the teaching in `docs/reference/configuration.md`. | `planner_authoring_aids.py`; the byte pin, shared with CDA-02 |
| CDA-05 | Corrected brief. The probe sends `planner_tool_definitions(dialect=…)` with no discovery policy, the unfiltered superset. Tests compute expected tools and strict counts from `planner_tool_definitions(...)`, not literals, so CDA-02's reworded declaration cannot break them. | None |
| CDA-06 | Corrected brief. Surface-aware copy in `web/credential_guard.py` over a closed set of model-output surfaces. A surface not in the set gets today's copy. The literal-copy pins on user surfaces stay. | None |

**Held from wave 1:** CDA-03, until S-3 is answered.

CDA-02 and CDA-04 both change model-facing text, so both may move skill-hash
consumers, the wire census and the scaffold byte pin. CDA-04 merges first.
CDA-02 merges after S-2, re-measured and re-verified on the tree it lands on.
Generated inventories are regenerated, never hand-merged.

**Blocked lanes float.** A lane waiting on a ruling does not hold its wave.
When it is unblocked, its file collisions are recomputed against the tree as
it then stands. CDA-18 edits `tool_batch.py` and `provider_gateway.py`
(shared with CDA-17, CDA-20, CDA-22); CDA-07b edits
`sessions/routes/_helpers.py`, `planning_application.py` and
`composer/service.py` (shared with CDA-09, CDA-20, CDA-22).

### Wave 2

1. **CDA-07a** (advisor capability record, END rubric, repair copy, blocker
   detail) and, in parallel, **CDA-18** once S-4 is answered. CDA-18 does not
   edit `advisor_checkpoint.py`.
2. **CDA-08**, after CDA-07a: it shares `advisor_checkpoint.py`,
   `composition_completion.py`, `planning_application.py`, `turn_effects.py`
   and `no_tool_policy.py`. P-B18 (the "final reply is unavailable" notice
   that stays in the transcript) is diagnosed here, since it is the same file.
3. **CDA-09**, after CDA-08.
4. **CDA-19**, after CDA-18, with the egress-redaction effect covered by
   negative controls.
5. **CDA-07b**, when S-5 is answered.

Before CDA-07a's acceptance: rebuild the S1 replay arm from captures made on
this base, because the 44 stored requests embed the old skill text.

### Wave 3 (serial)

The original plan ran CDA-10 and CDA-11 in parallel. On this base all four
lanes change `pipeline_planner.py` or the scaffold byte pin, so they run one
after another:

**CDA-11 → CDA-10 → P-B1 → CDA-12a → CDA-12b → CDA-13.**

- **P-B1** is new: decode a JSON-encoded `pipeline` at the provider boundary
  (ruled 2026-11-10). It follows CDA-10 because both edit the planner's
  terminal-payload handling. It has no brief yet. The brief must say what
  the audit record holds (raw or decoded arguments) and whether the 0.8.12
  argument-shape instruction is then removed.
- **CDA-12** takes the split its brief suggests. 12a: wiring, archive,
  advisor text, parity test, overlay, and unwinding the 0.8.12 orientation.
  12b: the CDA-R7, R8, R13 and R15 text. It also takes two things from P-A1
  that the CDA brief omits: state the session's approval mode in the request,
  and decide whether the MCP surface gets skill text. Use
  `reports/guidance-parity-design.md` and `reports/skill-prompt-audit.md` as
  design input.
- A same-day live baseline on this base is required before CDA-12's
  acceptance. The CDA numbers (129/23 mentions, 52.5k tokens, 32/110) and the
  prior attempt's battery are not comparable with each other or with this
  base.

### Wave 4

- **CDA-21 → CDA-23** (shared pin files). CDA-23 waits for S-7.
- **CDA-17**, after CDA-12 and CDA-13.
- **CDA-22 → CDA-20** (both now edit `composer/service.py`; CDA-20 also
  follows CDA-17). CDA-20 shrinks to the 429 classification plus
  `Retry-After` and the deadline check.
- **CDA-14** after S-6, **CDA-15** after its brief is rewritten for the closed
  roster, **CDA-16** after CDA-12 and after CDA-07b (the carry-forward
  changes the advisor history the cache investigation measures), or
  re-measured if it runs earlier.
- **CDA-03** whenever S-3 is answered; it collides with nothing.

### Design work, not yet lanes

- **P-A2** (one cumulative proposal) and **P-A3** (opted-out session loses
  the turn). The designs are lost. `reports/astra-followup-defect-research.md`
  §A and §B are close to designs and are the starting point. A redo must
  settle the five seams `NEXT-SESSION.md` lists, and must be reconciled with
  CDA-02's definition of "accepted" and CDA-23's state rows.
- **P-B12 and P-B9a** (about 100 registered rejection codes have no
  explanation for the model). Re-derive from the live registry; the lost
  recon is not needed.

### Waiting on a ruling, no wave yet

- **P-B17** (the tutorial brief creates the failed-rows output and the launch
  check admits it): S-1.
- **Batch plugins to general availability, with P-B11**: S-8.

### Not scheduled

P-B5 (blob pagination; a reviewed plan survives), P-B7 (`os.pidfd_open` on
Python 3.14; belongs with the 0.8.14 line, which makes 3.14 the only
interpreter), P-B9c, P-B13 (core half), the rest of P-B19a, the items of unknown status,
and the triage of `CHECKLIST.md`.

## 7. Execution

- Lane state is kept by the repo's `lane-manager` under `.lanes/` in the
  integration worktree, one run per wave, base ref
  `integration/composer-remediation`.
  (`.lanes/` is the tool's default and is gitignored; the baseline and recon
  working copies are under `.claude/lanes/`.)
- `verify` proves red on base, green on the merged tree, then runs the suite
  command on the merged tree. The suite command is the sweep in §5. `verify`
  runs are serial: one at a time, never while another broad suite is running.
- Each lane's brief and re-anchor section are copied into its lane directory,
  so a sandboxed worker does not depend on reading another worktree.
- At the end of each wave: the full-suite gate
  (`scripts/full-suite-gate.sh --execute --detach`) on the integration tree,
  and the keyless trust-tier finding set compared before and after.
- The host also runs CI jobs. One broad suite at a time; workers capped to
  leave them room.
- Nothing is pushed and no pull request is opened without the maintainer's
  say.

## 8. Not verified

- The recon was by code reading. No finding was reproduced by running the
  product on this base.
- The live evidence behind CDA-07, CDA-12 and CDA-16 was measured on the old
  build.
- Whether the 0.8.12 combined gates ended green rests on its pull request.
- The baseline test run on this base was still in progress when this was
  written. No `verify` result is trusted until the baseline's exit code and
  failure set for the full §5 selection are recorded in
  `.claude/lanes/baseline/` in the integration worktree.
- The peer review ran no tests either.
