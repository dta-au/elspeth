# Task 1 Restore secret wiring policy on proposal acceptance

Read AGENTS.md and CONTRIBUTING.md in the worktree before implementation.

## Goal and approved design

Implement audit Q1 / XC-03 / wire_secret_ref-N1, tracker 02.03. The user approved
commencing the existing audit remediation plan. Accept replay must derive the
secret wiring policy from the same server-authored WebSettings allowlist used by
normal dispatch. It must not accept policy from proposal/model arguments.
Use runtime_secret_wiring_policy(settings.secret_wiring_allowlist), following
src/elspeth/web/sessions/routes/_helpers.py, and pass it to execute_tool in
src/elspeth/web/sessions/routes/composer/proposals.py.

## Scope and constraints

- Preserve deny-by-default exact (secret, component_type, plugin, option_key) rules.
- Preserve provider authorship, independent execution acknowledgement, custody,
  review, preflight, session fencing and receipt-backed replay.
- No change to D1 staging policy, new settings, tool schemas, persistence schema,
  public error envelope, or unrelated refactoring.
- Use an actually supported plugin credential option for the positive route case;
  passthrough/api_key is NOT a valid runtime plugin configuration.
- Tests must exercise HTTP proposal acceptance and real tool dispatch/authorization,
  proving persisted accepted state and unauthorized refusal, not just a mock kwarg.
- Never include credential values in output or reports. No external provider calls.
- Use existing test factories, specced doubles only where necessary, no suppressions.

## Work location and interfaces

Worktree: /workspace/elspeth/.claude/worktrees/composer-remediation-wave1
Base: 6825994b482bc274df2ae85d8738b9078f0117b4 (detached task worktree)
Audit sheet (read-only main): /workspace/elspeth/docs/architecture/composer-tool-assessment/tools/wire_secret_ref.md
Production file: src/elspeth/web/sessions/routes/composer/proposals.py
Existing HTTP fixtures: tests/unit/web/sessions/test_routes.py, _make_app and
_create_test_composition_proposal; existing accept test starts around line 1178.
Prefer a dedicated focused test module under tests/unit/web/sessions/routes/composer
if the existing fixture imports and repository conventions support it.
Report: /workspace/elspeth/.claude/lanes/composer-remediation-wave1/task-1-report.md
Logs: /workspace/elspeth/.claude/lanes/composer-remediation-wave1/

## Steps

- [ ] Verify worktree import provenance with PYTHONPATH containing BOTH worktree/src
  and worktree/elspeth-lints/src; use .venv/bin/python.
- [ ] Read relevant current handlers and HTTP fixtures. Run the closest existing
  accept test as the baseline. Record command, log and exit code.
- [ ] Write regression tests: exact allowlist match accepts (200), persists the
  secret marker and accepted proposal status; absent policy and near-match rule
  refuse (422), settle rejection and preserve prior graph. Demonstrate test failure
  before the production fix because the route drops the policy.
- [ ] Apply the minimal policy construction/passing fix. Avoid test-only code in production.
- [ ] Run the new cases and affected accept/secret-policy suites. Demonstrate the
  positive regression detects removal of the policy kwarg (the pre-fix red run
  may provide that negative control if it exercises the same production path).
- [ ] Run ruff on touched files, check_contracts, Sessions/Composer attribute and
  session-mutation-authority gates if affected. Run relevant boundary tests where
  touched observation-boundary references require them.
- [ ] Self-review diff for real fixture validity, sensitive data, stale pins and
  authorization weakening. Do not launch broad pytest or PostgreSQL suites;
  parent owns that shared capacity and will run consolidated verification.
- [ ] Leave changes UNCOMMITTED for parent review packaging/integration. Write report
  with red/green evidence, exact commands/exits, changed files, limits and concerns.

Do not spawn agents. Return only report path, short status and test summary.
