# Task 3 Make expression operator parity detect missing standalone tokens

Implement audit Q25, tracker 13.16. Worktree:
/workspace/elspeth/.claude/worktrees/composer-remediation-discovery.
Read AGENTS.md/CONTRIBUTING.md and recon.md Q25 section in the shared lane.

Scope is test-only: tests/unit/web/composer/test_tools_discovery.py,
TestDiscoveryTools.test_expression_grammar_documents_every_operator. Existing
substring membership permits < to hide inside <=, > inside >=, and / inside //.

- [ ] Run the existing test baseline and demonstrate false negatives with controlled
  deletion of standalone <, > and / from the served Operators section.
- [ ] Tokenize actual documented operator entries, preserving multiword is not and
  not in as indivisible tokens. Keep AST-token completeness and safe-constant checks.
  Do not change production grammar/parser or introduce a second production authority.
- [ ] Run the improved real parity test against original grammar (pass) and controlled
  deletions of <, >, /, ==, is not and not in (each fails specifically for omission).
  Keep useful regression coverage without tests that merely mirror helper internals.
- [ ] Run the full test_tools_discovery.py module and ruff; parent owns broad suites.
- [ ] Self-review, leave changes uncommitted; do not edit other task files or spawn agents.

Report mutation controls, commands/exits, tests and concerns to
/workspace/elspeth/.claude/lanes/composer-remediation-wave1/task-3-report.md.
