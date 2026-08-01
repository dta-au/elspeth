# Acceptance-R2 remediation — final whole-branch review

Range `42366311c` (the build the acceptance run tested) → `68db8bc6d`
(release/0.7.2, all four streams merged). Produced by a whole-branch reviewer
plus four seam-specific cross-task reviewers (planner, advisor, settlement,
security). Companion to
[the plan](2026-08-01-acceptance-r2-remediation.md) and
[the run sheet](2026-08-01-acceptance-r2-runsheet.md).

## Verdict: SHIP WITH FIXES

No Critical findings and a coherent net tightening of the security posture,
but three Important cross-task interaction defects — each a composition of two
individually-clean tasks — plus one confirmed P1 residual must land first. All
are small and localized.

**Reconciliation suite:** 35,620 passed / 27 skipped / 1 xfailed / **1 failed**
(12m12s, `-n 12`). The failure is the runbook-contract test tripped by T21's
direct `aws logs delete-log-group`; fix in flight.

## Fix wave — must land before soft launch

1. **T9×T8 verdict-window fail-OPEN.** T9's rubric tells the END advisor to
   *quote* the user's constraints; T8's parser scans the first 5 non-empty
   lines. A quoted bare uppercase `CLEAN` in-window, with the real `FLAGGED`
   below line 5, parses as a **silent sign-off** — no format re-prompt fires.
   Silently defeats the gate T8/T9 hardened; untested interaction.
   *Fix:* whole-reply FLAGGED-dominance (fail-closed) or verdict-on-line-1 with
   window-misses routed through the existing re-prompt; fold in the parked
   case-insensitive FLAGGED widening and the stale `classify_signoff_verdict`
   doc reference (service.py:6459).
2. **T1×T3 contradictory repair instructions.** The nodeless nudge promises
   "re-emit unchanged and it will be accepted"; T3's satisfiability guard then
   rejects exactly that re-emit. Two contradictory repairs against a default
   budget of 2 → a guaranteed unrepairable path, the precise failure class this
   sprint existed to eliminate (ADR-031 canary doctrine).
   *Fix:* suppress the nudge (or fire the satisfiability guard first, its
   feedback naming the missing fields) when `unproducible_output_fields` is
   non-empty; add a two-defect budget test.
3. **Bare `ValueError` escapes the pair-salvage catch.** chat_solver.py:1580
   raises bare `ValueError` where siblings raise
   `GuidedToolArgumentShapeError`; in the mistyped-`on_validation_failure` pair
   case it discards a parsed-valid retain and mislabels it "unavailable" — a
   residual breach of R2-F15's (P1) own guarantee. Same pattern at
   chat_solver.py:213. One-liners; fix together.
4. **Runbook-contract failure** — in flight, reframed to add the missing
   "capture but tolerate one named expected error" capability rather than
   contorting the code or weakening the contract.

## Ships as tracked debt

Latent unexitable loop if an auto-wired splice ever fails validation (wants a
validation-parity probe); planner-authored placeholder-endpoint egress
(pre-existing, narrower than the server-inserted vector T11 closed);
human-channel exhausted detail can echo quoted fence sentinels (cosmetic);
incremental freeform edits never trigger auto-wire (pre-existing, backstopped
by the execution-time gate); auto-wired node ids title-case awkwardly on
review cards; plus 15 "acceptable" triage rows from the ledger.

## Security posture: coherent

One doctrine at four seams — blob paths sealed at write time
(`blob:<ref>`, can't-regress by construction), the advisor fence neutralized in
both directions from one helper, prohibited-plugin disclosure limited to name +
closed reason, and the aws_s3 three-way story (allowlisted / categorically
banned / boot-WARNed) legible and mutually consistent. Fix-wave item 1 is the
only security-relevant item this sprint *introduced*; with it landed, the
branch is a strict net tightening over the acceptance-run baseline.

## Epoch: correct and complete

`SESSION_SCHEMA_EPOCH = 41`, exactly one bump in range (8d3138fac, T18), zero
doc stragglers, drift now test-pinned. The single wipe-on-deploy covers every
persisted-shape change across all streams — including purging the historical
raw-path `composer_meta` rows T13 closed at the write boundary.

## Systemic observations

- **The failure mode is composition, not unit correctness.** All three
  Importants are two-task interactions where each half passed review clean.
  The per-task layer worked; this layer caught exactly what it structurally
  could not. The planner repair loop and the advisor verdict path each deserve
  a standing end-to-end contract test that stacked guards must pass *together*.
- **The repair budget is a shared resource nobody owns.** T1, T2, T3 and the
  nudges each assume they are its only consumer. Any future guard should come
  with a multi-defect budget audit.
- **Two defects escaped their tasks' scoped test sets** (T11's identity
  contract, T21's runbook contract) because the contract lived in an
  integration file outside the module's own suite. Seam-touching work should
  scope in the tests that pin the seam's contract.
- **Hygiene is good:** ~24.9k insertions, no TODOs, no defect-pinning skips,
  closed-vocab registrations agreeing across every layer checked, 35.6k tests
  green on first reconciliation.
