# Pause protocol: 2026-10-25 (the maintainer is pausing 0.8.10 for a few days)

> **RESUME TASK 0 COMES FIRST: make every test finish.** It is owned ONCE by named owners (u45-firstcompose for U-46 (b)/(a) and the reserve contracts; one coordinator-assigned owner for the suite-wide pytest timeout) and landed first. It is NOT solved per lane: every other lane resumes its own unit under the hang-proof guardrails. See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. Maintainer's order, 2026-10-25.


Every lane comes to a graceful halt at its next reasonable checkpoint, then gets ready for a cold start. Nobody pushes. Nobody lands anything on the target. The coordinator does the only push, a backup of the target.

1. **Reach a checkpoint.** Finish the atomic step you're on. Start no new test runs, Codex/Sol jobs or reviews.
   - A test run with under about 10 minutes left may finish. Stop anything longer yourself; its log is evidence of where you stopped.
   - Wait for a background Codex/Sol job to finish, or stop it cleanly. Save its output into your notes.
2. **Commit everything** in every worktree you own (`git status --short` must come back empty).
   - WIP commits are fine and expected. Mark them `wip(...)` in the subject. Commit by pathspec (`git commit -- <paths>`); never `git add -A` in a shared checkout.
   - Run `scripts/branch-safety-check.sh --intent commit` first. `--no-verify` is allowed only to preserve WIP on your dev branch when a hook rewrites files, and then you run the secret scan by hand.
   - Never stage `.claude/lanes/`. Source sitting under a gitignored path is a defect: move it onto your branch.
   - Do not rebase or rewrite history now. Leave branches exactly where they are.
3. **Write the cold-start notes** into your lane report under `.claude/lanes/release-0.8.10/` (your existing `<lane>-report.md`, or create one). Add a section headed `## COLD START (paused 2026-10-25)` covering:
   - unit(s) owned, branch @ tip SHA, worktree path(s), and the base it's measured against;
   - done and verified, with the evidence path for each claim;
   - in progress: exactly what is half-done, and what the WIP commits contain;
   - the next step, specific enough to start without reading the transcript (commands included);
   - open questions and rulings owed, by whom (coordinator or maintainer);
   - hazards: things that look true but aren't, stale pins, flaky ids, KNOWN HANG files;
   - landing conditions still owed (landing rule, keyless set-diff, gates).
4. **Leave nothing running.** `ps` for processes whose cwd is in your worktree(s) must show none: no pytest, codex, monitor or wait loop.
5. **Run `scripts/work-at-risk.sh`** and confirm it shows no uncommitted work in your trees.
6. **Report to the coordinator in one message:** branch @ tip, `git status --short` empty (yes/no), notes path, and processes left (none). Then stop and wait.
