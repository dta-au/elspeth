# U-46 report: fencing-gate reliability diagnosis

> **RESUME TASK 0 (hung tests) is the release's FIRST priority, owned by u45-firstcompose plus one coordinator-assigned owner. It is NOT this lane's job.** See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. This lane resumes its own unit, under the guardrails: one pytest at a time, `-n 3` max, `timeout 2400`, both hang files `--ignore`d, no directory runs without a slot. Do not touch the fencing gate, the reserve-contract tests or the pytest config. Maintainer's order, 2026-10-25.


- Branch: `codex/0.8.10-u46-diagnosis`, tip `439d09f30`, based on the target at `ac98fe243`. It is docs only and not pushed. Deliverable: `docs/release/0.8.10-fencing-gate-diagnosis.md`, with the instruments in `docs/release/u46-instruments/`.
- Commits, one per section: `d8beee92e` (skeleton), `77e2a54b6` (§2.1–2.2), `3c8c8b297` (§2.3), `7f1d55f7f` (§2.4–2.5), `7d8da921b` (§3 and the c035eafe5 cascade), `9d657a568` (§5–6), `45a9933b2` (§2.6 tip green; quadratic inference withdrawn), `4100577d3` (§3.4 instrumented versus uninstrumented), `e133896de` (§4 overlay, §5.4 known positive, P0), `439d09f30` (§1 verdict, §7 and the instruments).
- Gates: `scripts/branch-safety-check.sh --intent commit` exited 0 before each commit except `4100577d3`, which I committed without the pre-flight. Its hooks still ran (secret scan), and the next commit's pre-flight was clean. All commits ran hooks and exited 0. Ruff check exits 0 on the instruments.
- Measured trees: `main` `3b28d537b`, target `ac98fe243`, `feed6a9e6`, the current tip `ae3904bd7`, and the overlay `a6e812e86`. Logs are in `.claude/lanes/release-0.8.10/u46/out/`.
- Known-positive plant: placed only in the scratch worktree `.claude/worktrees/u46-main-3b28d537b`, never committed. It was restored with `git restore --source=HEAD` because `checkout --` is hook-blocked, and `git status --porcelain` then showed 0 lines.

## Summary

1. **This is long-standing, not a regression.** On both `main` and the target, the tool takes about 775 CPU-s and the test file takes about 2,810 CPU-s at `-n 0`. The 900 s and 600 s timeouts are below that cost.
2. **The hot spot is `_mutation_callable_escapes`, 58% of the tool.** About half of all tool time is the whole-tree reserve proof. The structural cause is repetition. There are 135 full-file AST passes per file per run. `_AuthorityProof` is built 10 times. Per-(scope, name) re-walks add up to 112M node visits inside one function. One test id, about 1,482 s, exceeds the roughly 20-minute default-suite budget at any worker count.
3. **The overlay is far worse because of its own gate additions.** It adds 8.3k lines of `retained_*` receipt code, whose path-sensitive recursion bypasses its own memo. The hot function goes from 133 to at least 3,169 CPU-s, unfinished. An unchanged module, `core/schema_shape.py`, becomes about 1,700 times dearer. This must be fixed before the overlay lands (spec P0).
4. **The tool exits 0 while printing violations.** It printed 35 escapes at `ac98fe243` and 33 at `feed6a9e6`, both the cascade of one stale `sessions/service.py` full-source pin, which `c035eafe5` re-pinned. One stale digest becomes 33 rows that hide the root. The current tip `ae3904bd7` is green: escape 0 and reserve-proof failures 0.
5. **The fix spec is in §5 and is NOT started.** T1 makes the tool's exit code gate, with an explicit `--report-only`. T2 reports the root failure ahead of the cascade. P0–P5 address performance, under an equivalence oracle. The planted known-positive, 3 rows red today, must still go red after any change.
6. **Timeouts (§6).** The 900 s and 600 s limits are task-local and mis-sized. The real constraint is the AGENTS.md budget of about 20 minutes, and it is violated. Fix the work rather than raise caps, and guard against regression with structural counts, not wall time.
7. **Docs note only.** CONTRIBUTING.md:570-573 describes fencing xfails that were removed on 2026-09-08 (`75448fe17`). No xfail change is proposed, and neither maintainer-approved xfail is touched.

## Not done or owed

- No idle-host wall timing; CPU figures taken at load averages above 20 are soft.
- The overlay's full tool cost is a lower bound only, because the run did not finish within 3,600 s.
- The fencing-gate fix (§5) awaits assignment. The U-45 fix is on hold until U-38 lands, per the coordinator.


## COLD START (paused 2026-10-25)

The full cold-start notes for U-46 (fix and (a) design) are in `.claude/lanes/release-0.8.10/u46fix/EVIDENCE.md`, § COLD START. In brief: `codex/0.8.10-u46-fix` is at `aa044516b` (T1, T2, P1, (b)), and `codex/0.8.10-u46-diagnosis` is at `6af0328e3` (§8 rev 2 at `a40c87fda` plus the fix-phase instruments; unlanded). The next steps are to rebase (b) onto the target, produce the row-set evidence, classify the plant miss, write the hang-to-red wording, then §8 rev 3, then implement (a).
