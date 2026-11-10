# Composer remediation: next work package

Written 2026-11-10 at the end of the 0.8.12 quick-wins day. This is the
deliberate pass that follows it. Every item names the report that holds its
evidence; all reports are in `reports/` beside this file.

0.8.12 took the fixes that were small and frequently hit. What is left is
mostly design work, plus a handful of small defects found too late in the day.

## A. Design work (do these first; they change what the model is told and how proposals behave)

Designs for A1 to A3 now exist, each written by a Fable lead directing Astra
and each independently reviewed. They are on branch `docs/composer-0812-design`
under `docs/architecture/composer-remediation/design/` (`*-design.md` with a
matching `*-design-review.md`). All three reviews say "sound with corrections".

**Reconcile them before implementing anything.** The three designs were
written in parallel and contradict each other at their seams:

- A2 and A3 both claim session epoch 79.
- A3 retires a stale proposal at write time (`head_advanced`); A2 retires it at
  compose start (`base_conflict`) and refuses `request_interpretation_review`
  while a proposal is pending, which makes part of A3's contract moot.
- A1 builds its first step and its metric on `approval_batch_not_atomic` and
  `approval_proposal_pending`, which A2 deletes; A1 and A2 give the session
  context object different shapes.
- A2's "safe stopping point" after step 3 is not safe in explicit-approval
  mode: a planner supersede can silently replace the user's pending plan.
- The `diff_pipeline` baseline (A3) is not reconciled with A2's working state.

### A1. Per-surface skill restructure

The planner receives the whole composer skill, and about 29% of it is not
actionable from the planner's tool surface. 0.8.12 fixed contradictions and
stale claims in place and did not restructure.

- Split the skill by surface: proposal planner, freeform loop, escape hatch,
  advisor. MCP currently gets no skill text at all; decide whether it should.
- State the session's approval mode in the request. Today the model learns it
  only from the first refusal, so the first multi-edit turn in approval mode is
  always refused once.
- Re-measure with the DeepSeek battery before and after, on reasoning tokens
  and rejections by code.
- Evidence: `skill-prompt-audit.md`, `skill-rework-report.md` (§11.6 item 13),
  `deepseek-planner-trace-findings.md`.

### A2. Approval redesign: one cumulative proposal

0.8.12 contains the defect and does not redesign it: in approval mode one
composition proposal may be pending and multi-edit responses are refused whole.

- Design a cumulative proposal that later edits amend, in both modes.
- Auto-commit mode is uncontained: if the user ignores the first proposal and
  keeps typing, each turn stages another (five pending observed live). Decide
  whether the newer proposal supersedes the older.
- Blob-only proposals (`update_blob`, `delete_blob`) are not counted and go
  stale behind an accepted composition proposal.
- Stale pending proposals must be rejected by hand; an invalid single
  incremental preview is still staged and accepted.
- Evidence: `approval-containment-report.md`, `approval-containment-review.md`,
  `astra-followup-defect-research.md` (item A), `deepseek-postmerge-probe.md`.

### A3. Opted-out sessions lose the turn (409 `stale_compose_state`)

Reproduced live twice on 2026-11-10 after the fixes. A state-ownership defect,
not a message defect.

- Evidence: `astra-followup-defect-research.md` (item B),
  `deepseek-followup-tool-probe.md`, `deepseek-postmerge-probe.md`.

## B. Small defects (each is a test-first fix with an independent review)

| # | Defect | Evidence |
|---|---|---|
| B1 | A provider that sends `pipeline` as a JSON-encoded string exhausts its repair budget. Decoding it at the provider boundary would remove the failure; it changes the planner-output contract, so it needs a ruling. | `skill-rework-report.md` §11 |
| B2 | `diff_pipeline` compares with the state at the start of the current request, so "what changed?" asked one turn after an edit reports zero. Decide the intended baseline. | `deepseek-postmerge-probe.md` |
| B3 | A file question on the first turn gets the canned "review cards" reply from `composer/protocol.py`. | `deepseek-followup-tool-probe.md` |
| B4 | Preview returns no rows. | `deepseek-followup-tool-probe.md` |
| B5 | Blob listing has no pagination. | `quick-wins-triage.md` |
| B6 | `get_plugin_assistance` takes `plugin_name` where sibling tools take `name`. | `deepseek-planner-trace-findings.md` |
| B7 | The web server does not boot on CPython 3.14.3 (uv build): `ProcessWatchdog` calls `os.pidfd_open` unconditionally while `requires-python` is `>=3.12`. Add a fallback or bound the range. | boot trace, 2026-11-10 |
| B8 | `remove_output` leaving edges is fixed and unit-tested, never exercised live. | `deepseek-postmerge-probe.md` |

| B9 | Three fixes written by Sol and held out of 0.8.12: `explain_validation_error` learns `canonical_schema` and five others; `_validate_transform_provider_config_path` ordering (may be a no-op: the `upsert_node` caller already returned the right error); a contradictory `invented_source` row refused on a verbatim bind. Committed as WIP on `fix/composer-0812-evening`, unreviewed, no valid broad test run. | `evening-fixes-0812-report.md` on that branch |
| B10 | Credential-shaped prose in model reasoning (`password: "..."`) loses the whole turn (`credential_material_rejected`). Refusing reasoning is a documented control (security-assurance pack F8, EV-227). A withhold-instead fix is on `fix/composer-reasoning-credential-withhold` with an adversarial review: safe, but it does not help Anthropic (signed thinking blocks still refuse), leaves no stored trace of a withhold, and leaves the assurance pack false. Needs a ruling, then one piece of work covering all three. | `credential-reasoning-withhold-report.md` and `-review.md` on that branch |
| B11 | `batch_replicate`: the model declares the node-created `copy_index` in the node's input schema; the `schema_contract_violation` fix text points at the source. Two of three live runs exhausted the repair budget. `batch_stats` assistance does not name its output fields. | `deepseek-batch-plugins-probe.md` |
| B12 | 94 registered rejection codes have no `explain_validation_error` entry (268 registered, 174 explained; 15 of the 94 are audit categories, 3 are dead). The planner never sees raw rejection messages, so a catalogue entry is the only help it gets. Recommended: split the registry into model-facing and not, with a test requiring an explanation for every model-facing code. | `rejection-code-explanation-recon.md` on `fix/composer-0812-evening` |
| B13 | `gate_expression_preview_memory_exhaustion` can fire only if the evaluator's constructor fails: `ExpressionParser.evaluate` wraps `MemoryError` as `unexpected_error`. Decide whether `evaluate` should let it propagate (a core change). The skill row at `pipeline_composer.md:1468` still says to declare numeric fields for every gate evaluation failure. | `gate-eval-kind-repair-report.md` |
| B14 | `fork_coalesce` used 57% more reasoning and one extra provider call on the final tree than on the morning baseline. Not investigated. | `deepseek-final-battery-0812.md` |
| B15 | Gate preview samples the first 8 KB of a CSV, so the last sampled row can be cut off and lack correctly spelt fields; a correct gate condition on a larger CSV is then blocked. A kind-aware rewrite of the `gate_expression_type_mismatch_against_source_schema` advice failed review on this and is being reworked on `fix/composer-gate-eval-kind-repair` (not in 0.8.12). | `gate-eval-kind-repair-review.md` on that branch |
| B16 | The skill tells the model that `llm_prompt_template` reviews are "backend-owned" and that it must "never surface" them (`pipeline_composer.md` lines 1162, 1313 to 1320, 1373). The intent is "the server raises this approval card automatically; do not raise a second one", but the wording reads as "keep the prompt from the user", and a model reply shown to the operator on 2026-11-10 said "`llm_prompt_template` is backend-owned — do NOT surface it" despite the register rule at lines 57 and 1374. Reword at the source to say who raises the card, and check the register rule still holds. In the tutorial walk the prompt card was raised by the server and gated on viewing the prompt, as intended. | operator report, 2026-11-10; tutorial walk below |
| B17 | The tutorial's Build step offers an optional green "Apply" (add error routing to a retention output). Accepting it adds a second sink, and `_tutorial_launch_blocker` (`composer/tutorial_service.py`, single-sink rule since July) then refuses "Continue to Run" with "does not match the supported tutorial plugin set". Needs a decision: admit a retention sink in the launch check, or change what is suggested for every session; a tutorial-only suppression is not allowed. | dev-box tutorial walk, 2026-11-10 |
| B18 | "The model's final reply is unavailable. Please retry your question." appeared as a system note after the Apply turn and stayed in the transcript. Not diagnosed. | same walk, session `e5f0f169` |
| B19 | After a failed compose and a page reload the tutorial shows no Retry, only "Ask it to continue in the chat" with an empty message box. A pending account is told "Invalid username or password" at sign-in. The audit step shows the run's start time in UTC as if local. It also shows "LLM calls 4" beside a three-row result with no explanation: the fourth is the provider's `runtime_preflight` reachability call. Operator ruling 2026-11-10: explain it in the tutorial's audit-step copy; no systematic change. | same walk |

## Operator rulings, 2026-11-10 (evening)

These settle items that were open below; where they differ, these win.

- **B10, credential-shaped reasoning:** withhold the reasoning and keep the
  turn, covering Anthropic thinking blocks, with a stored record of each
  withhold and the assurance pack (F8, EV-227) updated. "Make sure the model
  has a way out", in the operator's terms: do not just block the text. Tell
  the model plainly what was withheld or refused and why, and always leave it
  a way to hand back to the user and say "I have no meaningful way forward"
  with the reason. Historically the model was trapped retrying against
  controls it could not satisfy until the turn timed out. Treat this as a
  general requirement on every refusal the model can hit, and check for it
  when reviewing A1 to A3: no control may leave the model without a terminal
  "return to the user with the problem" move.
- **B1, stringified `pipeline`:** approved. Decode at the provider boundary;
  it is Tier-3 parsing, consistent with the tiering policy.
- **B2, `diff_pipeline` baseline:** compare against the last accepted state.
  Settle the definition of "accepted" with the A2 design.
- **B17, tutorial:** the tutorial brief asks the Composer to create the
  failed-rows output as part of the pipeline. `_tutorial_launch_blocker`
  must admit that output in the same change.
- **B19:** explain the preflight LLM call in the tutorial's audit-step copy.
- **Batch plugins:** move `batch_stats`, `batch_replicate` and
  `batch_experiment_compare` into general availability. Fix B11 in the same
  patch. When `file_inject` is ready, fold it in as generally available too.
- **`origin/fix/composer-tool-remediation`:** deletion approved (the
  lane-assets commit `a1cc53b6d`). Do it once 0.8.12 is on `main`.
- **`file_inject`** (`origin/claude/happy-cray-mbfmm1`): lands with the next
  patch, the night of 2026-11-11.
- **Context budget** (`origin/claude/lucid-lamport-wank6h`, session
  "4. Hysteresis"): lands Friday 2026-11-13 night or later, depending on
  progress. It carries a required new setting, so it needs an upgrade note.
- **0.8.12 and 0.8.11:** pre-approved through to `main`; merge commits.

## C. Operator decisions outstanding

- Decided 2026-11-10: the three batch plugins (`batch_stats`,
  `batch_replicate`, `batch_experiment_compare`) are authorised through the
  deployment allowlist, and are on the dev box. With them, all nine live runs
  of the three affected battery cases built with the intended plugin. Still
  open: the AWS ECS default allowlist carries no batch plugins at all;
  widening it is a decision about what a cold install exposes.
- The 30 trust-tier findings classified as correct code in
  `tier-adjudication-0812.md` await the judge pass before signing. No
  allowlist entry or signature was created.
- The lane-assets commit `a1cc53b6d` on `origin/fix/composer-tool-remediation`
  (11,685 files, 1.2 GB with LFS) should not ship. Removing it needs a
  force-push or a branch delete.

## D. Housekeeping

- Recalibrate thresholds to the working system, not the reverse. The planner
  scaffold byte allowance is 3,878 against an approved 3,494.
- `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists` took
  582 s in a worktree and about 5 s in a `git archive` extract. Unexplained.
- Two tests fail under `-n 4` and pass serially:
  `test_portable_supervisor.py::test_healthy_child_fails_closed_when_supervisor_is_killed`
  and `…::test_authentication_and_partial_startup_fail_closed[peer-flood]`.
- The alias-gate red seen once in a full-suite run did not reproduce on any
  frozen commit (`gate-reds-0812-report.md`). Investigate only if it recurs in
  a frozen run.

## Measured result of the 0.8.12 day

One DeepSeek round of the 19-case battery on the final tree against the
morning baseline, one run per case, one counting script
(`deepseek-final-battery-0812.md`). On the 16 cases the plugin authorisation
did not affect: reasoning tokens 145,651 to 83,786 (-42%), all output tokens
-38%, compose wall time 403 s to 289 s (-28%, indicative), prompt tokens +4%.
Input dominates the token count ten to one, so total tokens are flat. The
instrument defects listed in the A1 design (the planner byte ceiling, the
battery driver's skill hash) apply to any future before/after claim.

## How to work it

- Local set-up that worked: a worktree with its own venv built by
  `uv sync --frozen --all-extras --python 3.13` (a symlinked venv reports the
  main checkout's version and fails the DAG scenario goldens), plus
  `npm ci` at the root and in `src/elspeth/web/frontend`.
- The isolated DeepSeek probe and its reasoning tap are described in
  `deepseek-followup-tool-probe.md` and `deepseek-postmerge-probe.md`. It never
  touches the live service and never calls `/execute`.
- Every code change on 2026-11-10 had an independent review before or just
  after merge. The reviews found four defects the authors' own test runs
  missed, each time because the author had not run the file that pinned the
  behaviour. Run the whole of `tests/unit/web/composer`,
  `tests/unit/web/execution`, `tests/unit/web/sessions` and
  `tests/unit/architecture` for any composer change.
