# Acceptance R2 remediation — run sheet

Live status of the fix sprint executing
[2026-08-01-acceptance-r2-remediation.md](2026-08-01-acceptance-r2-remediation.md).
Baseline `95afb1898` (release/0.7.2). Four parallel worktrees; every task is
implemented by a fresh subagent and reviewed (spec + quality) before the next.

**Legend:** ⬜ not started · 🔵 implementing · 🟡 in review/fix loop · ✅ complete (reviewed) · ⛔ blocked

## Composer stream — worktree `r2-composer` (sequential: service.py is shared)

| Task | Finding | Sev | Status |
|---|---|---|---|
| T1 planner source-omission repairable | R2-F16 | P1 | ✅ complete (bf94d3a12) |
| T2 threshold-fidelity guard + coverage credit (amended) | R2-F17 | P1 | ✅ complete (0a210fef6) |
| T3 unsatisfiable 0-transform sketch | R2-F4 | P3 | ✅ complete (593439324) |
| T4 retain-and-defer wrong-stage intents | R2-F15 | P1 | 🔵 implementing |
| T5 transcript custody for stage prose | R2-F6 | P2 | ⬜ |
| T6 advisor rebuttal output contract | R2-F12 | P2 | ⬜ |
| T7 fence sentinels (leak + escape) | R2-F13 | P2 | ⬜ |
| T8 advisor parsing/retry/surfacing | R2-F14 | P2 | ⬜ |
| T9 END advisor sees user constraints | R2-F8a | P3 | ⬜ |
| T10 timeout salvaged-draft surfacing | R2-F9 | P2 | ⬜ |
| T11 auto-wire required guardrails | R2-F10 | P2 | ⬜ |
| T12 prohibited plugins discoverable | R2-F18 | P2 | ⬜ |

## Disclosure stream — worktree `r2-disclosure`

| Task | Finding | Sev | Status |
|---|---|---|---|
| T13 blob-path leak via implicit_decisions | R2-F11 | P2 | ✅ complete (a1730b45a) |
| T14 consent dialog external effects | R2-F7 | P2 | ✅ complete (0ec32fb32) |
| T15 request_id on error envelopes | R2-F16b | P2 | ✅ complete (f8fa2ca04) |
| T16 aws_s3 policy legibility (doc+WARN) | R2-F1 | P2 | ✅ complete (cc02578f4) |

## Forms/UI stream — worktree `r2-forms`

| Task | Finding | Sev | Status |
|---|---|---|---|
| T17 conditionally-required collision_policy | R2-F2 | P3 | ✅ complete (31fe8d4f9) |
| T18 review cards show transform options | R2-F3 | P3 | ✅ complete (b7d68fd4f) |
| T19 stale progressbar + loading affordances | R2-F5 | P4 | ✅ complete (bbbf34e6f) |
| T20 Textract schema doc + node-name labels | R2-F8b | P3 | ✅ complete (9619da6ae) |

## Terraform stream — worktree `r2-terraform`

| Task | Finding | Sev | Status |
|---|---|---|---|
| T21 Container Insights orphan tolerance | R2-D3 | P2 | ✅ complete (9790d6c0c) |

## End-of-sprint gates (after all streams merge)

| Gate | Status |
|---|---|
| Merge order: composer → disclosure → forms → terraform (`--no-ff`) | ⬜ |
| Single SESSION_SCHEMA_EPOCH bump 40→41 — CLAIMED by T18 (e64f3d463); T4 reuses it | 🟡 owned |
| Full `pytest tests/ -n 12` (~10 min) + `elspeth-lints check` on merged release/0.7.2 | ⬜ |
| Final whole-branch review (most capable model) | ⬜ |
| Local staging e2e smoke (elspeth.foundryside.dev): rebuild, wipe DB, Playwright-verify composer fixes | ⬜ |
| Rebuild images, redeploy stack (wipes sessions.db) | ⬜ |
| Full 10-exercise live re-run incl. ex 6/7/9/10 | ⬜ |

## Notes / decisions

- 2026-08-01: operator authorized local staging use (rebuild, DB discard, sudoers restart, Playwright) at my discretion — inserted as a pre-AWS smoke gate.
- 2026-08-01: operator: full suite ≈1 h serial / ≈10 min with 12 workers — scoped tests per task, ONE `-n 12` full run at reconciliation.
- 2026-08-01: T2 ADJUDICATED mid-flight: blocking degenerate-gate codes would have rejected ELSPETH's own documented fan-out idiom (implementer escalated with receipts). Replaced with a planner-side threshold-fidelity guard + non-blocking fan-out advisory; plan amended and pushed. Intent (no silent double-writes) preserved.
- 2026-08-01: T1/T13/T17/T21 all implemented; reviews in flight. T1 deviated from plan Step 3 with justification (existing `no_source_configured` coded rejection for freeform parity) — reviewer adjudicating.
- 2026-08-01: HAZARD found+fixed: shared venv editable install pointed at the old catalogue worktree (bare-python imports only; pytest was protected by pyproject pythonpath). Repaired to main checkout; all pytest evidence stands.
- (running log appended below as work proceeds)
