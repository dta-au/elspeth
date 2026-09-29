# Composer guidance landing handoff

This directory contains the complete source brief for the guidance landing on `release/0.8.1`. The branch `docs/composer-guidance-landing` was cut from release commit `9c2e8b17c` after the T1, S-02, Codex, R2, X1/X2, G3, and QR prerequisites landed. The old 5887 branches are not needed to execute this package.

Read `prompt.md`, then `guidance-review.md`, `astra-verification.md`, `slotting.md` § SLOTTING, and the later entries of `rulings.md`. Keep the live triage and evidence in `work-log.md`. `prompt.md` has been adapted to these tracked filenames and the already-created worktree. The copied source reviews otherwise retain their 2026-09-27 historical wording; current behavior must be remeasured as the prompt requires.

The one independent review pass and its finding are retained in `independent-review.md`; the repair and verification are recorded in `work-log.md`.

In copied historical documents, `<repo>` replaces the original absolute checkout path. References to ignored lane scratch files are historical evidence pointers, not inputs needed to perform the work. Their observations are superseded by the required fresh `elspeth validate` and `elspeth run --execute` measurements against this branch. The review's item descriptions, replacement drafts, gate procedures, priority rulings, and scope decisions are all retained here.

The worktree is `.claude/worktrees/composer-guidance-landing` relative to the main checkout. Its `.venv` symlink and both source roots were verified. The main checkout has unrelated dirty files; leave them untouched until the final fast-forward safety check. Do not push.
