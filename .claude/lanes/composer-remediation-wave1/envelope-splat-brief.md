# Repair prior diagnostic envelope-gate regression

Parent supplies exact base after D1 state-admission review/commit. Work only in
/workspace/elspeth/.claude/worktrees/composer-remediation-wave1. Read AGENTS,
CONTRIBUTING whole-tree gates, and baseline-envelope-gate-analysis.md first.
No agents, commits, broad suites or production changes.

The earlier remediation commit f598aa43d introduced a legitimate conditional
dictionary unpack to omit unowned/ambiguous error_code. The envelope scanner
cannot enumerate it, producing14 pre-D1 failures. This is our prior-remediation
gate regression, not unrelated upstream failure. Tracker12.07 is reopened.

Own only tests/unit/web/composer/test_tool_result_envelope_gate.py, preserving
the preceding D1 structural-test update. Add minimal recursive inline Dict and
IfExp support in _splat_keys using the existing dictionary walker. Forward
constants, resolver and dict_forwarded context; preserve prefix application,
attribution and existing cast/unknown-expression refusals. Enumerate both arms;
never suppress one refusing arm. Do not change inventories, fences, teaching
declarations, counts, allowlists, snapshots or production payloads.

Use TDD and the exact meaningful positive/negative matrix in the analysis:
both arms have unique keys; nested/prefixed mappings; constant/local/helper
context; dynamic keys/unresolved names/unattributed helpers/unsupported and
cast expressions in either arm must refuse. Real producer AST mutation must
plant an untaught key in each actual generation.py conditional arm separately
and fail for that key; include an envelope-homonym backstop. Prove original
scanner cannot pass the supported real syntax and new controls as appropriate.
Never treat a different earlier refusal as the intended mutant detection.

Run complete envelope gate plus relevant walker-authority tests and scoped
Ruff/format. Existing diagnostic catalogue/response tests should stay unchanged;
run their affected selection to substantiate behavior/byte compatibility. Parent
owns broad final tests for scanner infrastructure, so no full-suite launch.
Use both source roots, explicit cwd/import provenance, lane-private logs and
terminal exit files. Distinguish current14-failure baseline from final green.

Freeze and write envelope-splat-report.md and envelope-splat-review.diff in
the canonical lane, containing exact commands/exits, red/green evidence,
mutation controls, changed file scope and limitations. Return concise status.
Parent will obtain Astra specification/quality review before committing.

## Authorized follow-on within this repair

The expanded verification found one more omission from f598aa43d: two new
diagnostic code forwarding sites were not added to the existing explicit
reviewed-forwarder census. Parent inspected the actual producers and lookup:
error_text is emitted only after explain_validation_code's exact catalogue
lookup succeeds; matched_code is selected only from catalogue keys bound by
identity to the matched record. The catalogue-subset-of-registry pin remains.

Extend scope narrowly to tests/unit/web/composer/test_error_code_redaction.py:
review and add exactly the two (composer/tools/generation.py,
_execute_explain_validation_error, error_text|matched_code) tuples with concise
provenance comments. This is the census's intended reviewed-site workflow,
not authorization to loosen the walker, registry, suppression or code policy.
Add meaningful positive/negative controls proving only those exact reviewed
sites are accepted and changed expression/function/path remains unreviewed.
Keep the real-producer closed-code/ambiguous/unknown/runtime tests and subset
pin; no production edits or signature/fingerprint changes. Existing failure
is the red control. Re-run the failing census/new controls and affected full
inventory/diagnostic selection; do not repeat the expensive unchanged envelope
gate merely because this isolated census test file changes. Final report must
separate the first877pass1fail result from the corrected terminal evidence and
give both-file review diff. Astra reviews both changes together.
