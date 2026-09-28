# Composer guidance landing: live work log

## Custody

- Target: `release/0.8.1`; branch: `docs/composer-guidance-landing`; worktree: `<repo>/.claude/worktrees/composer-guidance-landing`.
- Cut from release commit `9c2e8b17c984264be618a06de33d8d7d8956be69` on 2026-09-29.
- Main checkout is dirty with unrelated AGENTS/docs changes; do not edit it before the final fast-forward.
- Both `elspeth` and `elspeth_lints` imports resolve to this worktree with the two-root `PYTHONPATH`.
- Source inputs: tracked `prompt.md`, `guidance-review.md`, `astra-verification.md`, `slotting.md` § SLOTTING, and `rulings.md` (all beside this log). Astra's corrected drafts and newer rulings control.
- Confirmed release ancestry: T1/S-02 merge `ce3dcc6b3`, S-02 carrier correction `ac30f001f`, field_mapper strict retirement `286be0b92`, and row-items teaching `5b70950f6` are all ancestors of this branch.

## Triage method

Read current text and record its file:line for every in-scope item. DONE means current release already teaches the measured rule; APPLY means a still-wrong surface takes the corrected draft; REWRITE means the review draft conflicts with since-landed behavior; DROP means the named surface no longer exists or the finding is outside this unit. Behavioral claims require an absolute-settings `elspeth validate` and, where runtime matters, `elspeth run --execute` before editing. No guidance source has been edited yet.

## Triage table

| ID | Current release surface and text | Verdict | Evidence / intended wording |
|---|---|---|---|
| S-01 | pending | pending | |
| S-02 | pending | pending | carrier-qualified aliases |
| S-03 | pending | pending | |
| S-04 | pending | pending | |
| S-05 | pending | pending | |
| S-06 | pending | pending | capability wording |
| S-07 | pending | pending | T1 |
| S-08 | pending | pending | |
| S-09 | pending | pending | |
| S-10 | pending | pending | |
| S-11 | pending | pending | union_field_collision only |
| S-12 | pending | pending | |
| S-13 | pending | pending | check guided surface still exists |
| S-14 | pending | pending | policy not Composer-authorable |
| T-01 | pending | pending | |
| T-02 | pending | pending | |
| T-03 | pending | pending | T1 |
| T-04 | pending | pending | small catalogue capability fact |
| T-05 | pending | pending | |
| T-06 | pending | pending | |
| T-07 | pending | pending | MCP roster constraint |
| T-08 | pending | pending | |
| T-09 | pending | pending | |
| T-10 | pending | pending | |
| T-11 | pending | pending | |
| R-01 | pending | pending | |
| R-02 | pending | pending | |
| R-03 | pending | pending | |
| R-04 | pending | pending | Codex fixes landed |
| R-05 | pending | pending | T1 |
| R-06 | pending | pending | |
| R-07 | pending | pending | |
| R-08 | pending | pending | |
| R-09 | pending | pending | |
| R-10 | pending | pending | |
| R-11 | pending | pending | |
| R-12 | pending | pending | |
| R-13 | pending | pending | |
| R-14 | pending | pending | union_field_collision only |
| P-01 | pending | pending | |
| P-02 | pending | pending | |
| P-03 | pending | pending | R2 routed missing_field |
| P-04 | pending | pending | capability wording |
| P-05 | pending | pending | |
| P-06 | pending | pending | T1 |
| P-07 | pending | pending | |
| P-08 | pending | pending | |
| P-09 | pending | pending | |
| P-10 | pending | pending | |
| P-11 | pending | pending | |
| P-12 | pending | pending | |
| P-13 | pending | pending | |
| P-14 | pending | pending | configured-instance catalogue facts |
| P-16 | pending | pending | |
| P-17 | pending | pending | |
| P-18 | pending | pending | |
| M-01 | pending | pending | |
| M-02 | pending | pending | |
| M-03 | pending | pending | |
| M-04 | pending | pending | strict option retired |
| M-05 | pending | pending | |
| M-06 | pending | pending | |
| M-07 | pending | pending | |
| M-08 | pending | pending | T1 |
| B-16 | pending | pending | |
| B-17 | pending | pending | |
| B-18 | pending | pending | |
| B-19 | pending | pending | |
| B-21 | pending | pending | |
| B-22 | pending | pending | |

Out of scope: P-15 and B-23 (merge 2), B-20 (no supplied finding), and all other IDs not selected by SLOTTING.

## Measurements and gate results

Pending. Record commands, absolute settings paths, exit codes, and frozen-tree evidence here.
