# Composer remediation: next work package

Written 2026-11-10 at the end of the 0.8.12 quick-wins day. This is the
deliberate pass that follows it. Every item names the report that holds its
evidence; all reports are in `reports/` beside this file.

0.8.12 took the fixes that were small and frequently hit. What is left is
mostly design work, plus a handful of small defects found too late in the day.

## A. Design work (do these first; they change what the model is told and how proposals behave)

### A1. Per-surface skill restructure

The planner receives the whole composer skill, and about 29% of it is not
actionable from the planner's tool surface. 0.8.12 fixed contradictions and
stale claims in place and did not restructure.

- Split the skill by surface: proposal planner, freeform loop, escape hatch,
  advisor. MCP currently gets no skill text at all; decide whether it should.
- State the session's approval mode in the request. Today the model learns it
  only from the first refusal, so the first multi-edit turn in approval mode is
  always refused once.
- Re-measure with the DeepSeek battery before and after, on reasoning tokens
  and rejections by code.
- Evidence: `skill-prompt-audit.md`, `skill-rework-report.md` (§11.6 item 13),
  `deepseek-planner-trace-findings.md`.

### A2. Approval redesign: one cumulative proposal

0.8.12 contains the defect and does not redesign it: in approval mode one
composition proposal may be pending and multi-edit responses are refused whole.

- Design a cumulative proposal that later edits amend, in both modes.
- Auto-commit mode is uncontained: if the user ignores the first proposal and
  keeps typing, each turn stages another (five pending observed live). Decide
  whether the newer proposal supersedes the older.
- Blob-only proposals (`update_blob`, `delete_blob`) are not counted and go
  stale behind an accepted composition proposal.
- Stale pending proposals must be rejected by hand; an invalid single
  incremental preview is still staged and accepted.
- Evidence: `approval-containment-report.md`, `approval-containment-review.md`,
  `astra-followup-defect-research.md` (item A), `deepseek-postmerge-probe.md`.

### A3. Opted-out sessions lose the turn (409 `stale_compose_state`)

Reproduced live twice on 2026-11-10 after the fixes. A state-ownership defect,
not a message defect.

- Evidence: `astra-followup-defect-research.md` (item B),
  `deepseek-followup-tool-probe.md`, `deepseek-postmerge-probe.md`.

## B. Small defects (each is a test-first fix with an independent review)

| # | Defect | Evidence |
|---|---|---|
| B1 | A provider that sends `pipeline` as a JSON-encoded string exhausts its repair budget. Decoding it at the provider boundary would remove the failure; it changes the planner-output contract, so it needs a ruling. | `skill-rework-report.md` §11 |
| B2 | `diff_pipeline` compares with the state at the start of the current request, so "what changed?" asked one turn after an edit reports zero. Decide the intended baseline. | `deepseek-postmerge-probe.md` |
| B3 | A file question on the first turn gets the canned "review cards" reply from `composer/protocol.py`. | `deepseek-followup-tool-probe.md` |
| B4 | Preview returns no rows. | `deepseek-followup-tool-probe.md` |
| B5 | Blob listing has no pagination. | `quick-wins-triage.md` |
| B6 | `get_plugin_assistance` takes `plugin_name` where sibling tools take `name`. | `deepseek-planner-trace-findings.md` |
| B7 | The web server does not boot on CPython 3.14.3 (uv build): `ProcessWatchdog` calls `os.pidfd_open` unconditionally while `requires-python` is `>=3.12`. Add a fallback or bound the range. | boot trace, 2026-11-10 |
| B8 | `remove_output` leaving edges is fixed and unit-tested, never exercised live. | `deepseek-postmerge-probe.md` |

Three more were in progress when this was written (`explain_validation_error`
not knowing `canonical_schema`; `_validate_transform_provider_config_path`
gate ordering; an `invented_source` row admitted on a verbatim bind). If
`evening-fixes-0812-report.md` exists and they are merged, they are done;
otherwise they belong in this table.

## C. Operator decisions outstanding

- Web-authorise the three hidden batch plugins (`batch_stats`,
  `batch_replicate`, `batch_experiment_compare`) or keep them hidden. The
  skill text currently does not mention them.
- The 30 trust-tier findings classified as correct code in
  `tier-adjudication-0812.md` await the judge pass before signing. No
  allowlist entry or signature was created.
- The lane-assets commit `a1cc53b6d` on `origin/fix/composer-tool-remediation`
  (11,685 files, 1.2 GB with LFS) should not ship. Removing it needs a
  force-push or a branch delete.

## D. Housekeeping

- Recalibrate thresholds to the working system, not the reverse. The planner
  scaffold byte allowance is 3,878 against an approved 3,494.
- `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists` took
  582 s in a worktree and about 5 s in a `git archive` extract. Unexplained.
- Two tests fail under `-n 4` and pass serially:
  `test_portable_supervisor.py::test_healthy_child_fails_closed_when_supervisor_is_killed`
  and `…::test_authentication_and_partial_startup_fail_closed[peer-flood]`.
- The alias-gate red seen once in a full-suite run did not reproduce on any
  frozen commit (`gate-reds-0812-report.md`). Investigate only if it recurs in
  a frozen run.

## How to work it

- Local set-up that worked: a worktree with its own venv built by
  `uv sync --frozen --all-extras --python 3.13` (a symlinked venv reports the
  main checkout's version and fails the DAG scenario goldens), plus
  `npm ci` at the root and in `src/elspeth/web/frontend`.
- The isolated DeepSeek probe and its reasoning tap are described in
  `deepseek-followup-tool-probe.md` and `deepseek-postmerge-probe.md`. It never
  touches the live service and never calls `/execute`.
- Every code change on 2026-11-10 had an independent review before or just
  after merge. The reviews found four defects the authors' own test runs
  missed, each time because the author had not run the file that pinned the
  behaviour. Run the whole of `tests/unit/web/composer`,
  `tests/unit/web/execution`, `tests/unit/web/sessions` and
  `tests/unit/architecture` for any composer change.
