# Tier burn-down — Wave 1 handover (2026-08-28, hub session "justify-burndown")

Plan: `docs/superpowers/plans/2026-08-28-tier-model-justify-burndown.md` (+ `.buckets.json`).
Epic: `elspeth-3ab6107b1f`. Branch: `feature/unified-lineage`. Wave-1 base `ff917243a` → tip **`53f6c10d8`**.

## State at handover

- **All 23 Wave-1 buckets merged** (`--no-ff`, one merge per lane, `git log --first-parent ff917243a..53f6c10d8`): B01–B23 (B02/B08/B10/B23 ran as one lane "BS1"). Two post-wave merges on top: audit fix `2fbf161e9` (F1/F2) and hygiene `53f6c10d8`.
- **Result:** ~300 justify findings removed by honest code change, **187 rationalised** (sidecars `docs/agents/sweeps/tier-burndown/*.rationales.json`). Whole-tree L1 corpus is 2, both rationalised (B03/B04).
- **Full suite** running in background at handover: worktree `.claude/worktrees/wave1-suite` @ 53f6c10d8, `-n 16`, log `<scratchpad>/wave1-full-suite.log` (ends with `exit=N`). **Not yet read.** Whoever resumes: read the summary line + exit, then `git worktree remove .claude/worktrees/wave1-suite`.
- **Filigree bucket issues (23 + BS1 + hygiene + audit) are OPEN at `in_progress`** — close them only after the suite is green (`mcp__filigree__issue_close`, commit anchor `feature/unified-lineage@53f6c10d8`). Bug `elspeth-a1ab69607a` is at `verifying` → close too.
- **JOHN'S INSTRUCTION: PAUSE at end of Wave 1.** Do not create Wave-2 issues/worktrees/lanes until he says go. Close-out remaining: suite result → close issues → report.
- No worktrees other than `wave1-suite` remain. No other session state.

## Expected suite caveats (not regressions)
- Composer authoring tests that poison the litellm import (`generation.py:1816`, `planner_authoring_aids.py:1868`) may go red: B15 made a broken-but-installed litellm propagate instead of reading as absent — fix the fixture, not the plugin.
- `immutability.freeze_guards` shows 3 findings (`chat_parts.py:117`, `reference_join.py:167`, `web/execution/validation.py:256`) — **pre-existing on `ff917243a`**, verified; obs `elspeth-obs-838cda5b95`.
- `fingerprint_baseline.json` was regenerated (3350→4128 entries); its consumer test now passes rather than xfails.

## Audit results (John's ask — repeat before every wave sign-off)
Two independent lenses over `ff917243a..a4f633728` (comments 8724 lens A, 8726 lens B on the epic). Cross-corroborated:
- **F1 FIXED** (`2fbf161e9`): B21's `except (TypeError, AttributeError, KeyError, NameError): raise` in langfuse tracer + B18's guard removal = SDK signature drift aborted rows. Re-raise set is now exactly `TIER_1_ERRORS`. Memory: `feedback_audit_control_location_before_tests_bake_it_in`.
- **F2 FIXED** (tests): decoder "envelope is divergent" refusal pinned for all cross-action pairs.
- S1: ~50 `isinstance`→`type() is` across 17 files = implicit closed-union commitment for ~10 owned types — documented in `recent-code-hints.md` (hygiene commit `41eca6141`).
- Lens B recommends the same audit for Wave 2, especially B26–28 and B38/B54.

## Lessons that change Wave-2 briefs (LANE_BRIEF.md needs these baked in)
1. **Boundary decorators suppress ONLY R1/R5** (`contracts/trust_boundary.py:73`). R2–R4/R6–R9 = real code or rationale. (I briefed this wrong for B21; B22 caught it.)
2. Rationale keys: `<file>:<RULE>:<symbol>:ast=<path>` — worklists must carry fingerprints next time (mine dropped them; corpus is now MIXED `ast=`/`fp=`; reconcile at re-stage by file:RULE:symbol).
3. Before/after evidence = save full raw corpus (allowlist disabled via `--allowlist-dir <empty>`) and `comm`/`diff`, never totals. My first regex undercounted paths with digits.
4. `type(x) is C` is the house idiom for scalars, NOT for container/union discriminators whose else-branch matters (B16 S3 estimator hold; B11 mypy narrowing) — check the else branch.
5. Any plugin source edit → re-pin `source_file_hash` + scenario-corpus cascade in dependency order (`recent-code-hints.md` 2026-08-26 entry); PH3 gate `plugin_contract.plugin_hashes`; strict recompute via `scripts/cicd/plugin_hash.py`.
6. Dynamic-attribute site touched → reseed `config/cicd/masquerade_baseline.yaml` same commit; merge conflicts there are usually both sides deleting entries — drop both, `seed_baseline --check`.
7. `recent-code-hints.md` conflicts are additive — keep both sides (happened 5×).
8. **Never reclaim a lane worktree until the lane confirms nothing is running** (B15 incident; memory `feedback_ping_a_lane_before_reclaiming_its_worktree`). Idle notification ≠ done: lanes go idle waiting on background pytest.
9. Task-type Filigree issues have no review state (open→in_progress→closed); lanes leave at in_progress, hub closes after the wave gate. Some lanes forget to claim — check assignee.
10. Lane deliverable = Filigree comment; some lanes post the comment but never message the hub — check `comment_list` before pinging.
11. Cap: 8 lanes × `-n 2`; a held lane's slot can host a small extra lane (BS1).
12. Wave-2 hot spots: B38 `guided/planning.py` (178, 133×R1) and B54 `plugin_policy` (121) = ONE parse boundary per payload type, not 100+ edits; B56 `sessions/service.py` (13.6k LOC) solo in Wave 3; B26–28 acceptance harness = boundary decision first (Wave 4).

## Judge policy change (f0e38838d)
Control-location claims are now a named fault class in the judge's static policy (`elspeth-lints/src/elspeth_lints/core/judge.py`, decision question 7) + two corpus cases. `JUDGE_POLICY_HASH` changed → **operator must re-run the real-LLM `check-judge-quality` gate** (`config/cicd/judge-quality-corpus/README.md`) before the next `sign-bundle`; agents cannot (needs Codex/key). Annotate briefs: name controls by symbol + test nodeid, never line numbers.

## Open items (tracker)
- `elspeth-6a9eb088c6` P1 bug: `sink.py` runtime_checkable Protocol as dispatch control — own lane (edits masquerade test doubles).
- `elspeth-fdf115047f` P3: six Wave-1 code residuals (lease-owner dup, S3 spool leak, Textract binding parity, json_expand missing text_field routing, `_events_attempted` lock, stale docstring).
- `elspeth-8d46db34ff` P2: tier_model lint precision (try/except join; post-init R5 exemption) — AFTER the re-stage (moves signed findings).
- `elspeth-a881fce8bc` P3: split provider config models from clients (L1 residual).
- `elspeth-23ee8e3440`: `stage_status` paste-ready command must learn `--lanes`/`--continue-on-block` — land BEFORE the re-stage.
- Hub-owned at re-stage: dead signed entries obs `elspeth-obs-c12a90675a` (4), `elspeth-obs-0dbb251f06` (allow_hits[159] steps.py), B11's 3 removed already-signed sites in `state_guard.py` → `stale_delete` lane; rationale-authoring rule: cite symbols/excerpts, never line numbers (`elspeth-obs-c6998fc57d`); B01's known-weak rationale `_require_bounded_positive_int` — if judge BLOCKs, escalate to `require_int` TypeIs, don't re-word.

## Re-stage sequencing (unchanged)
Fix waves → **one** `stage_scan` (after the LAST fix-lane merge; every commit re-stales) → `stage_annotate` from the merged sidecars (reconcile key forms) → `stage_preview` → operator fires `sign-bundle --lanes resign,new_judgment --continue-on-block`.

## Scratchpad artefacts (session-local; will vanish)
`<scratchpad>/LANE_BRIEF.md`, `CONTROL_AUDIT_BRIEF.md`, `worklists/B*.md`, `raw_tier.txt`, `empty-allowlist/`, `wave1-hub-notes.md`, `wave1-full-suite.log`. Regenerate worklists for Wave 2 from `.buckets.json` + a fresh raw scan at the new tip (bundle keys from the stale bundle are still useful for symbol lists).
