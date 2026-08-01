# Acceptance-R2 sprint — handoff

For the instance orchestrating the engine-refactor merge. Written 2026-08-01 at
`18c06d49f` on `release/0.7.2`.

## State: sprint work is done and merged; verification is deliberately incomplete

All 21 remediation tasks, the 3-item post-review fix wave, and one
operator-endorsed advisory are landed on `release/0.7.2` and individually
reviewed. Working tree clean.

| Artifact | Where |
|---|---|
| Plan (with the T2 amendment) | `docs/superpowers/plans/2026-08-01-acceptance-r2-remediation.md` |
| Live run sheet | `docs/superpowers/plans/2026-08-01-acceptance-r2-runsheet.md` |
| Final whole-branch verdict | `docs/superpowers/plans/2026-08-01-acceptance-r2-final-review.md` |
| SDD ledger (per-task history, fix rounds, parked rulings) | `.superpowers/sdd/2026-08-01-acceptance-r2-remediation/progress.md` (git-ignored) |
| Per-task reports | same directory, `task-N-report.md` |
| Cold-install agent prompt (run 3) | `docs-archive/acceptance/agent-prompt-aws-cold-install-r3.md` (offline by design) |

**Verification status:** `elspeth-lints check` passed. The full
`pytest tests/ -n 12` was re-running at handoff — the *previous* full run on the
same tree was **35,620 passed / 1 failed**, and that one failure (T21's runbook
contract) is fixed in `853dd2dfb`. Staging smoke, redeploy and the live
10-exercise run were **deliberately not started**, at the operator's
instruction, so the engine refactor folds into one verification pass.

## What the refactor merge should watch

The sprint concentrated on two subsystems the refactor is likely to touch.

1. **`src/elspeth/web/execution/validation.py`** — now holds T2's
   control-coverage credit (`input_fields_unprovable`) *and* the registration
   of two advisories. If the refactor rewrites this file, the questions are
   **semantic, not textual**: is `input_fields_unprovable` still reachable, and
   are `gate_fan_out_advisory` / `static_llm_prompt_advisory` still non-blocking
   *and* registered on every discriminator surface? Precedent: when two upstream
   chips merged mid-sprint, the real work was proving reachability, not
   resolving conflicts.
2. **The planner repair loop** (`composer/pipeline_planner.py`) — four guards
   now fire in series (T1 sourceless, T3 satisfiability, T2 threshold, the
   nodeless nudge) against a **shared repair budget of 2** that no single guard
   owns. The final review flagged this explicitly: any new or reordered guard
   needs a multi-defect budget audit, because two guards firing in sequence can
   exhaust the budget before the model ever sees a full validation summary.

**Two defects this sprint escaped their tasks' scoped test sets** because the
contract lived in an integration file outside the module's own suite — T11's
finalizer identity contract (`test_shared_planner_surfaces.py`) and T21's
runbook contract (`test_aws_ecs_runbook_contract.py`). Seam-touching work should
scope in the tests that pin the *seam's* contract, not just the module's.

## Outstanding, in priority order

1. **Reconcile the engine refactor**, then re-run `pytest tests/ -n 12` + lints.
2. **Staging smoke** — the local service runs from this checkout
   (`/etc/systemd/system/elspeth-web.service`, `WorkingDirectory=/home/john/elspeth`).
   Epoch is **41**, so wipe `sessions.db` before restart (`auth.db` never).
   `sudo systemctl restart elspeth-web.service` is NOPASSWD-whitelisted.
3. **Rebuild + redeploy AWS**, then the **full 10-exercise live re-run** using
   the run-3 prompt above — it includes the four exercises the last acceptance
   run never reached (chained gates, `on_error` quarantine, fork/coalesce, and
   an invented pipeline).
4. **Trust-tier signing ceremony** — operator-fired; agents stage key-free only.

## Open items filed for others

- **`elspeth-b35b10722c` (P1)** — the in-flight **LLM source** plugin will
  bypass required-control coverage and auto-wiring: the capability walk is
  node-only (`coverage.py:245,267`, `required_controls.py:464`), so an LLM
  source emits model output with no `content_safety` demanded on a deployment
  that requires it. Needs a parity sweep before that plugin lands.
- **`elspeth-6bdb7e7736`** — once the LLM source plugin exists, revisit the
  `static_llm_prompt_advisory` string to name the real plugin id (it currently
  asks *"did you mean to use an LLM source instead?"* without naming one,
  deliberately, since none exists yet).
- Tracked debt from the final review: validation-parity probe for auto-wired
  splices; planner-authored placeholder-endpoint egress; human-channel sentinel
  neutralization; incremental-edit auto-wire gap. See the final-review doc.

## Environment gotchas that cost time this sprint

- The shared `.venv`'s editable install points at **one** worktree's `src` and
  drifts. `pytest` is protected (`pyproject` sets `pythonpath`), but bare
  `python` — especially snapshot/baseline **regenerators** — can silently read
  or write from the wrong tree. Verify with
  `python -c "import elspeth; print(elspeth.__file__)"` or prefix
  `PYTHONPATH=$PWD/src`.
- Full suite: ~1 h serial, ~12 min at `-n 12`. Don't launch it while a fleet of
  subagents is running.
- Trust-tier CI is **exempt** for this sprint by operator directive (signature
  drift from churn is expected); every other gate is in force.
