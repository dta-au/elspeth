# Cache-hit audit envelope — tracker 11.15

Implement the bounded group 3 fix already mapped in next-remediation-candidates.md.
User authorized ongoing remediation and behavior tests. Base is reviewed local
f9c23c59c63bbd3099029e995a778bade594f6c8, containing D1 and Landscape repair.
Parent inspected the current cache-hit branch in tool_batch.py: provider JSON
uses the complete admitted/normalized current result, while finish_success
receives a hand-built success/data/cache_hit mapping. This drops current
version, validation, affected_nodes and other legitimate envelope fields.

## Intended change

Record the same owned admitted/normalized result envelope delivered to the
provider. Reuse the established audit serialization path where appropriate;
do not create a second independently assembled approximation. Keep cache
provenance in ComposerToolInvocation.cache_hit. If retaining the old payload
cache_hit field is actually needed, identify that compatibility requirement
and test equality after removing only that explicitly audit-only field.
Preserve historical slim audit result readability. Do not mutate old records.

No cache key, reread, budget, progress, handler invocation, provider routing,
admission authority, redaction, proposal, persistence or lifecycle policy change.
No fallback that hides malformed cached producers or turns readmission faults
into successful responses. No public MCP cache/path changes.

## Tests first

Extend actual cache-hit producer tests in test_service.py and
test_compose_loop_audit_wiring.py. Drive discovery, real state mutation, then
cache hit; compare exact delivered envelope with the audit result and durable
private evidence. Verify current validation/version, affected components,
unchanged discovery data, one handler call, two invocation rows, miss/hit flags,
Pydantic response serialization and existing prompt-guard handling. Preserve
private response hashing/redaction. Exercise admitted response contracts and
legacy cacheable results only if both are still reachable in the live registry;
measure that registry rather than inventing a fake public tool.

Preserve or add negative controls: malformed cached payload/readmission fault
has no synthetic success/provider egress and no duplicate persistence. Obtain
a meaningful failing regression against the unchanged current branch before
production edits. Existing tests intentionally asserting slim payload shape
must change only alongside the behavior fix.

## Ownership and verification

Exclusive tree: composer-remediation-cache-audit. Other agents own D2 path
admission and lifecycle foundations in separate worktrees; do not edit their
files. Expected own files are tool_batch.py and these cache/audit tests. Ask
parent before expanding implementation scope or altering source bindings.
One focused -n0 process permitted; no broad suites/all-rule scans, child agents,
installs, commits, pushes, Page edits or shared environment mutations. Use both
worktree import roots, explicit command/log/exit receipts, project-local test
temporary directory, scoped Ruff/mypy and appropriate affected contract gates.
Freeze final code/tests before review. No suppressions, signatures or blanket
pin refresh. Existing ordinary full-suite and PostgreSQL gates remain required
on the final combined candidate before integration.

Read AGENTS.md and CONTRIBUTING.md before edits. Write cache-audit-report.md
and a complete cache-audit-review.diff (including new files, if any) in this
lane. Report exact behavior, evidence, limits, changed paths and any remaining
failures; return report paths with a concise summary. Parent obtains Astra high
review and integrates only after findings are resolved.
