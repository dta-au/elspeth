You are landing ONE package on ELSPETH release/0.8.1: the composer guidance (planner teaching) update from the 5887 lane's guidance review. Land it end to end — worktree, edits, gates, fast-forward — and stop before pushing. This is the only work you do: no side quests, no new findings beyond this package's scope, at most ONE independent review pass.

## Inputs (read these first)
All are tracked beside this prompt in `docs/plans/2026-09-29-composer-guidance-landing/`:
- `guidance-review.md` — the original review: every item (S-*, T-*, R-*, P-*, M-*, B-* ids), its surface and file:line, a proposed replacement text, and the section "Gates the updater must regenerate or respect" (G-01..G-21, with the exact procedure for each).
- `astra-verification.md` — Astra's independent verification. Where Astra says CONFIRMED-AMEND or supplies a corrected draft, ASTRA'S DRAFT WINS over `guidance-review.md`. Use Astra's priority column.
- `slotting.md`, section "SLOTTING of the guidance findings after Astra's independent verification" — the authoritative item list for this unit (P1 / P2 / P3 / conditional lists) and five BRIEF RULES (below).
- `rulings.md` — binding operator and lane-owner rulings; newer sections amend older.
- `work-log.md` — the current triage, decisions, measurements, and gate results.

## The review is stale in places — re-verify every item against the CURRENT release first
The review was written on 2026-09-27 against a lane head. Since then all its prerequisites have landed on release/0.8.1 (T1 expression typing and S-02 via the B3 package `ce3dcc6b3`/`ac30f001f`; C1-C3; R2; X1/X2; G3; QR; field_mapper `strict` retired in `286be0b92`), and some teaching lines already landed with their fixes (e.g. `row | items | list`, the G3 template-API wording, the multi-query / row-call teaching, strict's retirement). So, BEFORE editing, build a triage table in your notes file: for each in-scope item id → current text at its surface on release (file:line) → verdict:
- DONE (release already teaches it correctly — cite the line),
- APPLY (still wrong/missing — apply Astra's draft, or GUIDANCE-REVIEW's where Astra did not amend),
- REWRITE (the behaviour changed since the review — write the text to the CURRENT behaviour, measured; e.g. P-03 must teach R2's routed `missing_field` behaviour, NOT "missing required input stops the run"; M-04 must teach that `strict` is gone; S-02 must teach the landed rule — an alias resolves only when a source can carry it — not "both spellings always resolve"),
- DROP (no longer applicable — say why).
Measure behaviour claims with a real `elspeth validate` / `elspeth run --execute` (ABSOLUTE --settings path) before writing teaching that states them. Teaching that states a behaviour the code does not have is the defect this package exists to remove.

## Scope
- IN: SLOTTING's P1, P2, P3 lists; the "conditional on T1" items (S-07, R-05 split, P-06, M-08, T-03's post-T1 sentence) and R-04 (codex-final has landed) — all prerequisites are now met; S-06 / P-04 (replace every plugin inventory claim or count with capability wording, e.g. "emits one row per buffered row; supports passthrough and transform"); R-14/S-11 for `union_field_collision` only (the policy is not authorable in the composer).
- CODE that the teaching needs, in this package, ONLY where SLOTTING says so: T-04 (publish the catalogue capability fact the teaching relies on) and P-14 (configured-instance facts in the catalogue) — facts derived from class declarations, authoring nothing. If either spreads beyond a small, self-contained change, commit the teaching without it, and report exactly what it would take.
- OUT (merge 2): P-15, B-23. OUT: anything not in the SLOTTING lists.

## BRIEF RULES (from Astra's surface map — binding)
1. JSON-schema field/knob descriptions are STRIPPED before the proposal planner (planner_authoring_aids.py ~1363, ~1432): a description-only fix does not reach the planner. Fix hints / skills / aids, and check the planner projection actually carries the text.
2. Include the surfaces reviews usually miss: selected-schema evidence rehydration, terminal instructions, notices, direct tool-loop context messages.
3. The MCP roster is 32 tools vs the web's 42 — MCP has no inspect_source / emit_pipeline_proposal and carries no skill instructions; do not teach MCP what it cannot do.
4. Repair feedback keeps detail for only 3 codes (pipeline_planner.py ~2696-2710); know which text actually reaches the planner.
5. COMPOSER INVARIANTS (AGENTS.md, non-negotiable): the LLM does the job — teach and refuse, never author, synthesize or route pipeline structure server-side; no tutorial-special paths. Validation, rejection and redaction are fine.

## Gates (`guidance-review.md` "Gates the updater must regenerate or respect", G-01..G-21)
Follow each gate's stated procedure exactly (generated goldens via the test's own serializer, runtime_rejection_parity via `scripts/cicd/runtime_rejection_parity.py --write` then adjudicate, plugin source_file_hash via `scripts/cicd/plugin_hash.py`, scenario-corpus manifest literals and registry digests re-captured from the failing test's own output, soft-mapping census via `scripts/check_contracts.py --write-census`). Never hand-compute a hash or digest; never edit `validation_guidance_legacy_patterns.json` (G-04). Keep the aids digest within its size budget (G-08). Line numbers in the gate table are from 09-27 — locate by name.
G-01: do not edit `src/elspeth/web/composer/skills/*.md` while a test suite or elspeth-web is running (skill-hash-mismatch); tell the operator elspeth-web needs a restart after deploy.

## How to work
- Worktree: this branch already exists at `.claude/worktrees/composer-guidance-landing`, cut from `release/0.8.1` at `9c2e8b17c`; its `.venv` symlink is already set. Never edit the main checkout; never git stash.
- Env: `export PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src`; pytest as `<repo>/.venv/bin/python -m pytest -o 'pythonpath=<wt>/src <wt>/elspeth-lints/src' ...` (pythonpath ini is whitespace-separated); verify `elspeth.__file__` is inside the worktree. Logs to a private dir; read exit codes explicitly; never pipe a suite through tail.
- Keep the tracked `work-log.md` current (triage table, decisions, measurements, gate results) so a restart loses nothing.
- Commit in small logical commits by pathspec: `scripts/branch-safety-check.sh --intent commit && git commit -F <msg> -- <paths>`; trailer "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" as the last line; never amend. If a commit is blocked ONLY by the trust-tier ratchet hook, do not skip it — report the files.
- CHANGELOG: one entry under 0.8.1 describing the teaching update (user-visible: the composer now teaches X/Y/Z correctly); no per-item noise.

## Verification and landing
1. Focused: every test file named in G-01..G-21 plus the composer suites you touched (tests/unit/web/composer/, test_composer_runtime_agreement.py, catalog/golden tests), `-n 12`.
2. One independent review pass (a red-team or Codex reviewer) on the diff against the triage table: every APPLY/REWRITE item's new text is true of the current code (spot-check with real runs), no composer invariant is broken, no gate was hand-computed. Fix real findings once; do not loop.
3. Full gate: `scripts/full-suite-gate.sh --execute --detach --workers 12 --stages ruff,mypy,contracts,lints,pytest,testcontainer`; poll the printed .done; read summary.txt (frozen=yes). lints exits 1 by standing policy (non-fatal). Attribute any red: re-run it on a clean `git archive` of release/0.8.1 extracted OUTSIDE .claude/ (some whole-tree walkers skip .claude paths); only reds that pass on release are yours.
4. Land: merge the current release tip into your branch if it moved (resolve with the Edit tool; re-run the tests the new release commits reach), then in the main checkout `scripts/branch-safety-check.sh --intent merge` and `git -c rerere.enabled=false merge --ff-only docs/composer-guidance-landing` into release/0.8.1. If the main checkout has uncommitted edits to files the landing changes, STOP and ask the operator to commit or park them. Do NOT push — report the landed sha and ask.

## Return
The landed sha (or where you stopped and exactly why); the triage table (id → DONE/APPLY/REWRITE/DROP, one line each); code changes (T-04/P-14) or why deferred; gate results with exit codes; review findings and dispositions; a note that elspeth-web needs a restart for skill changes.
