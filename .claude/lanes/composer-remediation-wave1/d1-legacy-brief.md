# D1 legacy acceptance and terminal UI feedback

Worktree: /workspace/elspeth/.claude/worktrees/composer-remediation-wave1
Base: 5e192342c3fb025fd9a91244bebe437158e9de8e. It is clean/detached and contains Q1 policy fix.
Read AGENTS.md and CONTRIBUTING.md whole-tree gates before edits.
User authorized D1: known refusals/no-ops returned directly, only admitted changes staged; pending no-op proposals terminal. Every changed behavior requires regression tests. This task implements plan T5+T7; canonical settlement and staging are separate tasks and must remain explicitly open.

## Scope

Production src/elspeth/web/sessions/routes/composer/proposals.py and frontend/src/stores/sessionStore.ts (under src/elspeth/web). Tests: a focused route sibling using real test_routes helpers; existing accept/preflight/blob acceptance suites; sessionStore.test.ts. Narrow supporting type edits only if needed; no SQL/schema/lock authority edits.

- Catch ToolArgumentError from real legacy execute_tool replay into terminal rejected structured422, with safe argument diagnosis and error_type proposal_argument_invalid. Catch only this expected typed exception, not generic ValueError or integrity faults.
- After existing required-control/review finalization, successful ordinary graph result with composition_content_hash equal to current must terminally reject with error_type proposal_no_change and explicit system:no-change attribution. This includes handlers that increment version on an identical edit. Do not serialize/save a new state or run unnecessary runtime preflight for no-op.
- Preserve existing failed-result validation handling, real changed acceptance, base-stale409, transient runtime-preflight500/pending, missing-row integrity faults, terminal conflicts and cancellation draining. No new lifecycle status. Preserve exact server-owned secret policy, provider-authorship and required controls.
- Receipt-first blob replay and actual update/delete effects with unchanged graph are durable successes, not no-ops. Reuse existing external-effect distinction; inspect other possible store-only tools before assuming graph equality means no durable work. Do not change canonical pipeline path or immediate auto-commit semantics.
- Existing pending no-op/malformed proposals must get one terminal lifecycle event and no graph write. Repeat accept stops before execute_tool and cannot create another event. Competing terminal transition retains its evidence; missing proposal row after successful load must still fault. Cancellation must drain required legacy rejection/lease cleanup and preserve original failures.
- Frontend reloads proposals for both new terminal422 types in addition to proposal_validation_failed; no pending card after immediate refresh/reload. Keep true stale409, transient failures, session generation guard, refresh-failure semantics and validation error details.

## Behavioral tests and verification

Write red tests first through real HTTP/dispatch/persistence (existing factories and supported plugins, no fake preflight). Parameterize successful same-version no-op, empty metadata patch, same-value metadata edit that increments version, and real changed counterpart. Assert status/type, rejected attribution/event cardinality, unchanged state ID/history, no runtime-preflight for no-op, no redispatch on second accept. Add real malformed tool-argument case and generic unexpected-error control. Test cancellation/terminal race with exact named exception controls using existing appropriate authority fixtures, not handbuilt invalid owned records. Add frontend terminal-refresh tests, session-switch guard and existing stale/transient counterparts as applicable.

Baseline and focused regressions with BOTH roots: PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest -n0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" ... . Print import provenance. Existing test_proposal_accept_secret_wiring.py, test_proposal_accept_preflight_failure.py, test_blob_proposal_accept.py, relevant test_routes accept/reject cases must remain green. Run focused frontend npm test selection and typecheck (inspect package scripts first), changed Python/TS lint/format, scoped mypy and contracts. Parent owns broad/full tests and allrule corpus; avoid duplicate expensive whole-tree scans. Log exit codes explicitly; do not claim pending runs green.

No subagents, commits, pushes, broad suite or unrelated cleanup. Preserve existing untracked audit docs/main checkout. Freeze code on completion; write d1-legacy-report.md and d1-legacy-review.diff (include new files) in this lane. Report red/green commands, exact tests and route coverage, event/cancellation limits, compatibility changes and unresolved concerns. Return concise DONE/DONE_WITH_CONCERNS/NEEDS_CONTEXT/BLOCKED + report path.

Source-grounded compatibility ruling: full store-only family is excluded from graph-no-op detection, but only existing approval-required update/delete receipt paths are accepted at unchanged graph version. Historical manually staged create_blob has no supported proposal effect receipt and retains its existing409/pending boundary after its durable effect; normal create_blob remains immediate and never staged. Do not fabricate receipt support or classify its actual effect as no-change. Pin/document boundary rather than closing a receipt-support task.
