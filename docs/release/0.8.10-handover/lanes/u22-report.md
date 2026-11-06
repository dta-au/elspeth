# U-22 — PromptShield provider seams (report)

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.


Branch `codex/0.8.10-u22`, worktree `.claude/worktrees/u22-20261025`,
cut from overlay tip `codex/0.8.10-continuation-overlay` (60b5b27b9, fast-forwarded
to 7a713186d before any commit). Spec: `docs/architecture/design-notes/0.8.10-u22-promptshield-seams-design.md`
(Astra-reviewed, with the seam-7 addendum). Logs: `<worktree>/.claude/lanes/u22/`.

## 0. Baseline (before any change, overlay tip 7a713186d)

- `provider="server"` count: `git grep -c 'provider=.server' HEAD -- src` = 1.
- Tutorial routes + `test_routes.py::test_accept_refuses_server_provider_proposal_provenance`: pass.
- **The A9 census is red at the overlay tip, before U-22.**
  `test_composer_real_provider_transitions.py::test_each_real_composer_transition_has_its_own_settled_provider_evidence[initial|recompose]`
  fail (operation settles `operation_failed`). The same file passes at target ae3904bd7 (6/6).
  The root cause was surfaced with a scratch hook on `project_composer_operation_error`:
  the test's scripted SDK asserts `'emit_pipeline_proposal'` is among the offered tools.
  The overlay carries U-30's H2, which retires the empty-state planner route, so those
  transitions now run the ordinary compose loop and its palette. This is a stale
  fixture against H2, owned by the overlay/U-30, not an invariant break. The provider
  is still called. Reported to the lead.
- `tests/unit/web/composer` at 7a713186d: 157 failed / 13,587 passed (the overlay's own reds).

## 1. Seam 1 — F1: complete long graph values reach Advisor review

**Cause (verified at 7a713186d):** `advisor_checkpoint._require_complete_graph_summary`
raised `AdvisorGraphCoverageUnavailable` whenever the complete-value manifest was
nonempty, so EARLY/END withheld with zero provider calls (END then terminal-blocks)
and manual hints failed. `materialize_advisor_complete_values` had no production caller.

**Fix:**
- `fit_advisor_checkpoint_summary` keeps the bounded diagnostic display unchanged.
  It then resolves every manifest value through `materialize_advisor_complete_values`,
  which is now its first production caller and runs from the current credential-screened
  projection under the current scope.
- It appends a separate complete-review section, in manifest order and labelled by
  owned path. Each value is one whole untrusted JSON string token under the normal
  redaction policy. The section is appended after the display's identity spans are
  fixed, so their offsets stay exact and equal authored strings gain no exemption.
- `AdvisorSummaryParts.complete_values` carries the materializations. It is validated
  against the manifest exactly and in order.
- Incomplete coverage is a specific preparation refusal. The producer raises
  `AdvisorGraphCoverageUnavailable`; the checkpoint withholds with zero calls.
  `_require_complete_graph_summary` now checks exact coverage instead of presence.
- The fit runs per attempt, so each retry re-resolves and is inspected afresh.
- Resolution grants no inspection, detector or publication permission. Values over
  the 10,000-character inspection limit stay indivisible and are refused by the final
  guard (422, no SDK call), never sliced.

**Evidence:**

| Item | Result |
|---|---|
| F1 acceptance red (b9ed317fc), EARLY/END/manual × short/long | exit 1, 3 failed / 3 passed (long cases fail on the F1 withholding; short controls pass) |
| F1 acceptance green, plus a format_retry entry | 8 passed |
| Coverage oracles: complete-attack / complete-clean (two long values) | 2 passed |
| M1: restore the nonempty-manifest rejection | F1 acceptance 4 failed |
| M2: carry values but do not render them | 4 failed |
| M3: disable all coverage checks with a dropped value | incomplete-coverage cases 5 failed |
| M4: render only the first value | both oracles failed |
| `tests/unit/web/composer` at the seam-1 tip | 153 failed / 13,597 passed; **0 new** vs the overlay's 157 |
| ruff, ruff format, mypy src/elspeth, attribute contracts, check_contracts (census unchanged) | all exit 0 |

**Pins changed (they certified withholding):** `test_advisor_graph_visibility.py`
(`_diagnostic_graph` / `_complete_values` helpers and 4 tests), `test_content_projection.py`,
`test_advisor_checkpoint.py` and `test_prompt_guard_producer_parts.py`. Tests that parse
the display now use `raw_decode`, because the complete-review section follows the display.
The zero-call pins now cover genuinely incomplete coverage, via a dropped resolution.
The 10,001-character case now asserts the final guard's 422 refusal instead of withholding.

**Sessions dir and A9 (seam 1, on target ba4b25ee2):**
- `tests/unit/web/sessions`: the 9 failures new at my tip (`-n 4`) reproduce 8/8 under
  `-n 0` and fail identically at the overlay base under `-n 0` (lease expiry and preparation
  timeouts at host load 33–41). Load-sensitive, not seam 1.
- A9 at base ba4b25ee2 and at the seam-1 tip: the census, tutorial routes and
  `provider='server'` rejection give 25 passed at both. All 6 census transitions pass,
  identically; the census pins each transition's fixed provider-call count. The
  `provider="server"` count is 1 at both. For graphs with long values, EARLY/END regain the
  provider call that F1 withheld; the F1 acceptance test proves it.
- The branch was rebased onto ba4b25ee2 (old tip at `preserve/0.8.10-u22-seam1-pre-rebase`)
  and re-verified: the seam-1 files give 463 passed, and mypy is clean.

## 2. Seam 2 — F4: an unsupported graph is a classified, permanent refusal

**Carrier:** `ComposerContextUnsupported(ComposerAdmissionRefused)` in `composer/protocol.py`.
- Closed reasons: graph_resource_limit, graph_coverage_unavailable, prepared_request_bytes,
  manual_advisor_allowance, retained_history_bytes, retained_chunk_bytes. Each has a
  value-free public detail; module import fails if the two sets disagree.
- HTTP 422, `error_type` composer_context_unsupported.
- One permanent `failure_code` "context_unsupported", with the reason in its own field.
- Process-local evidence: partial state, undrained tool invocations, LLM calls and failed-turn
  metadata, via `with_evidence()`. These are ordinary attributes, because `attach_llm_calls`
  assigns `exc.llm_calls` on any `ComposerServiceError`.

**Owners:**
- `prompts.build_composition_content_view` (primary and Advisor) and
  `tools/_common.prepare_bounded_pipeline_state` (planner) translate the view's
  `CompositionViewUnsupported`, chained from the original.
- The discovery tool keeps its `complete_value_unsupported`.
- `tool_batch` records a manual Advisor context refusal as a semantic refusal, never PLUGIN_CRASH.
- The service's manual and EARLY promotion carry undrained evidence.
- `composer_turn` has a dedicated arm ahead of the admission arm. It persists outstanding
  audit, publishes progress (reason `context_unsupported`, no administrator advice) and
  raises the 422.
- `ComposerProgressReason` gains `context_unsupported`.
- SPA: the decoder and type union accept the new reason, and `isComposePermanentRefusal`
  hides Retry for it. The failure-outcome page records `error_type` None, as for the
  credential and admission refusals.

**Evidence:**

| Item | Result |
|---|---|
| Owner tests red (scaffolding only) | 4 failed / 1 passed, each on the raw `CompositionViewUnsupported` |
| Owner tests green | 5 passed (matched 3,000-char control succeeds) |
| Route test (`/messages`, `/recompose`, real async settlement) red / green | 2 failed (403 == 422) / 6 passed incl. admission controls |
| Manual dispatch (fault injection), EARLY-after-mutation (fault injection) | pass; M5 (drop tool_batch arm) red, M6 (drop EARLY promotion) red |
| vitest `config/composer.test.ts`, `api/composerOperationDecoder.test.ts` | red 2 failed / 20 passed, green 22 passed; tsc app+oidc and eslint exit 0 |
| mypy on the touched sources | clean |

**Fixture note:** the first fixture's 234 KB `prompt_template` values hit an earlier,
unclassified `ValueError` ("Template exceeds 16384 UTF-8 bytes",
`extract_jinja2_fields` via `summarize_pipeline_for_advisor`). An earlier rejection cannot
establish the F4 red, so the bytes moved into a plain option.
**Separate finding:** a graph whose `prompt_template` exceeds the engine's 16 KiB template
limit crashes the Advisor summary with a raw `ValueError` instead of a reviewable or
classified outcome. Not fixed here; recorded for a decision.

**Fencing:** `_reserve_CANCEL_CONSUMERS` pins `sessions/composer_turn.py` by whole-file
SHA-256. It was re-pinned twice, in c3c3a461d and then 67b6519c3, to measured values;
only the new except arm changed. The full reserve proof at 67b6519c3: RESERVE_PROOF_PENDING.

**Not done in seam 2 (reachability limits stated):**
- Hatch propagation: the planner, including its hatch, has no production caller after
  H2 (see the boot-probe finding).
- The title path builds no graph view.
- Neither route reaches an oversized starting graph past primary preparation, which
  refuses first; the manual and EARLY owners are proven by fault injection, not by HTTP.

## 3. Boot-probe parity (4 ids, U-30 chokepoint)

After the overlay `pipeline_planner.plan_pipeline` has no production caller
(`planning_application.py` deleted in fa9e91575). The boot probe still builds
`planner_tools`/`hatch_terminal` surfaces. The parity tests treat any `max_tokens` request
as "the planner request", which is now the compose loop, hence the four reds. Three planner
mutation-control tests pass vacuously, a gate failing open. No transition dropped to 0
calls. Reported to the lead with a recommendation; awaiting a scope decision.

## COLD START (paused 2026-10-25)

### Units, branches, trees
- **U-22**, PromptShield seams, parked behind U-30 by the lead's order (U-30, then the seam-2
  landing rebase, then the 16 KiB fix, then seam 4). Branch `codex/0.8.10-u22` @ `d0dcfc321`,
  worktree `.claude/worktrees/u22-20261025`. It forks at `dcb742ecc`; the
  target is now `d7ad18596`. Lane notes: `<u22 worktree>/.claude/lanes/u22/`.
- Older trees from Phase A: `composer-carries-20261024` (branch codex/0.8.10-composer-carries
  @ 0f4cd8aed), `composer-carries-base-20261024` and `composer-carries-gate-20261024`, both
  detached. All are clean and none is needed now; clean them up with
  `scripts/worktree-cleanup.sh` after the pause.
- U-30 is in progress: see `u30-report.md`.

### Done and verified
- Seam 1 (F1) landed as `2d41a085a`, squashed by the lead. Evidence is in §1 above.
- Seams 2 and 6 (F4 carrier, manual allowance), commits 35cd739e7..451eec473 on this branch.
  Evidence is in §2 above. The lead has reviewed them as the right shape. They are not
  landed, and the reserve proof was never obtained (KNOWN HANG; the fencing escape walk does
  not finish).
- §7 universal prepared-request cap. It was done out of order, my error, and is parked:
  - `4916ff479`: the cap at `_litellm_acompletion` for every role, at-cap and +1 tests per
    role in `tests/unit/web/composer/test_provider_preparation.py`, and settings tests.
  - `d0dcfc321`: dispatch-time refusal settlement. It found and fixed a dropped-evidence
    defect.
  - Evidence: `<u22 lane>/cap-green2.log`, `cap-e2e3.log`, and `PARKED-section7.md`
    (open items).

### Next step (after U-30)
1. Landing rebase: `git -C .claude/worktrees/u22-20261025 rebase release/0.8.10-assembly`.
   Pin `preserve/0.8.10-u22-pre-target-rebase` first. Never use `--onto … c338a79cd`.
   - Expect a conflict in `tools/_common.prepare_bounded_pipeline_state`: seam 2 edits it,
     and U-30 deletes it.
   - Re-run the seam-2 evidence. Use the reviewed full-source guards, not the hanging fencing
     walk.
2. The 16 KiB `prompt_template` fix (lead ruling, item 3 of the "Seam 2 is good" message):
   - write a failing-first test;
   - make it a classified, value-free refusal where the template enters Advisor summary
     preparation;
   - check validation and preview for the same raw ValueError, and fix the root cause once;
   - keep the limit.
3. Seam 4 (PreparedProviderInput), including the parked §7 commits. Re-run the §7 callers
   and gates listed in `PARKED-section7.md`.

### Owed
- Codex Daybreak and Astra reviews of U-22 before hand-off.
- Keyless set-diff for U-22.
- U-24 (seam 3 and seam 6 acceptance suites) is not started.

### Agreed order (lead, confirmed 2026-10-25)
U-30 first. Then the seam-2 landing (plain `git rebase release/0.8.10-assembly`, never
`--onto … c338a79cd`). Then the 16 KiB `prompt_template` fix. Then seam 4, including §7
(`4916ff479`, `d0dcfc321`).

### Does d0dcfc321's evidence fix stand alone? Mostly no: split it this way
- **The evidence half stays with §7.** It adds ComposerContextUnsupported to the primary loop's
  `with_evidence` arm, which wraps only the gateway call. Measured at 451eec473: seam 2 raises the
  refusal only from `prompts.py:405` and `tools/_common.py:3864`.
  - `_build_messages`, which reaches prompts.py, runs once at loop entry (`service.py:2471`),
    before any dispatch, so there is no earlier evidence to drop.
  - Tool-level refusals and EARLY are already carried with evidence by seam 2's own owners.
  - Only the §7 cap raises from inside the gateway call after earlier calls.
- **The progress half does stand alone.** It adds the refusal to service.py's
  `except (PromptGuardRefused, …): raise`, so the generic `service_setup_failed` progress event is
  not published before the route's classified one. That applies to seam 2's loop-entry
  `graph_resource_limit` too.
  - Pull it into the seam-2 landing as its own commit, with a red-first test driven by a seam-2
    oversized graph, not by the cap.
  - The current test, `test_primary_refusal_after_a_dispatched_turn…`, is cap-driven and stays
    with §7.

### Test capacity hard rules (AGENT-BRIEF.md, maintainer order)
- One pytest per lane at a time, at most `-n 3`, every run under `timeout 2400` or less.
- Directory runs need a slot from the coordinator.
- Always pass `--ignore` for test_web_landscape_mutation_fencing.py and
  test_web_reserve_reviewed_contracts.py.
