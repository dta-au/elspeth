# Task 2 Branch-complete diagnostic match codes

Implement GS-02, tracker 12.07, using current audit and recon evidence.
Worktree: /workspace/elspeth/.claude/worktrees/composer-remediation-discovery.
Read AGENTS.md and CONTRIBUTING.md. Read recon.md GS-02 section in this lane.

## Requirements

When explain_validation_error matches a closed catalog code, return its canonical
error_code on exact, regex and fuzzy branches. Preserve existing first-regex-match
precedence and Expected-hint augmentation. Use the existing guidance index as
authority; do not infer ownership by comparing arbitrary prose or choose an
unrelated code found elsewhere in the input. Unknown inputs and the runtime-only
union_field_collision reason must not invent a closed code. No new argument,
catalog coverage expansion, policy or unrelated refactoring.

## Files and checks

Production: src/elspeth/web/composer/tools/generation.py.
Tests: tests/unit/web/composer/test_tools.py::TestExplainValidationError,
test_validation_guidance_catalogue.py, test_generation_response_contracts.py,
test_discovery_response_contracts.py. Existing fixture:
tests/unit/web/composer/fixtures/generation_response_wire_4f206253.json
(confirm actual location before use). Regenerate via existing producer/serializer,
never hand-edit expected bytes without producer evidence. Existing contract permits
optional error_code; do not silently broaden redaction policy.

- [ ] Establish affected test baseline using BOTH worktree Python source roots.
- [ ] Add red tests for exact code, lowercase noisy code, uppercase noisy code,
  identifiable legacy prose, competing regexes and Expected hints; include
  unknown and runtime-only controls. Demonstrate actual pre-fix failures.
- [ ] Make minimal producer changes; run tests green and inspect fixture delta.
- [ ] Run affected tests, ruff and contracts/attribute gates as applicable. Parent
  owns the already-running exhaustive masquerade/mock/session gates on the frozen
  Q1 tree; avoid duplicating those expensive scans. Do not introduce reflection or
  unspecced mock sites. Parent will check any input changes before reusing evidence.
- [ ] Self-review. Parent owns broad suite and independent review. Leave changes
  uncommitted; no agents or external provider calls. Do not edit Task 1 files.

Report full red/green evidence and exact commands/exits to
/workspace/elspeth/.claude/lanes/composer-remediation-wave1/task-2-report.md;
return only short status and report path. Logs stay in that lane.
