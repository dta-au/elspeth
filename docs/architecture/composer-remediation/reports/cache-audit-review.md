# Cache-hit audit envelope review — tracker 11.15

## Reviewed candidate and method

Reviewed the requirements in `cache-audit-brief.md` first, then the implementation report and the entire frozen review patch. Applied the requesting-code-review criteria, including reasonable operator expectations, separate requirements/code-quality assessments, and severity calibrated to actual effects.

- Candidate: `/workspace/elspeth/.claude/worktrees/composer-remediation-cache-audit`.
- HEAD/base verified: `f9c23c59c63bbd3099029e995a778bade594f6c8`.
- Review artifact SHA-256: `e13501b914043dd7dd177b32deb5fcdc031503fc2aba366032b8ede6b9e05024`.
- The candidate diff produced the same SHA-256. It contains exactly the reported three tracked paths; the untracked-file listing was empty.
- Current hashes of all three changed files match `final-freeze.sha256`. The recorded before/after tracked-tree manifests also compare equal.

Inspected all changed hunks, their producer tests and fixtures, and surrounding consumers: cache extraction/readmission, admitted and legacy serialization, validation normalization, success canonicalization, prompt-guard content parts, private evidence projection/hash validation, display redaction, persisted outcome classification, and cache budget/progress control flow. Read AGENTS.md and relevant CONTRIBUTING.md conventions. This was a read-only candidate review; no tests, scans, installs, agents, commits, staging, or source edits were performed. Only this lane report was written.

## Requirements assessment

**Compliant with the bounded task.**

The change at `src/elspeth/web/composer/tool_batch.py:1646` obtains the audit payload from the owned admitted/normalized result's established `to_dict()` projection. `serialize_admitted_discovery_result()` uses that same projection for provider JSON. This removes the independently assembled slim approximation, preserving current validation, version, affected nodes, and any other fields emitted by the current envelope. The legacy branch selects `ToolResult.to_dict()` consistently with its existing provider serializer. Projection remains inside the existing cache-failure audit boundary.

`finish_success(..., cache_hit=True)` remains unchanged, so provenance stays in `ComposerToolInvocation.cache_hit`. New result payloads match the delivered envelope exactly. Historical records are neither rewritten nor made dependent on the new keys. The added historical-row test at `tests/unit/web/composer/test_compose_loop_audit_wiring.py:1273` exercises success canonicalization, existing redaction, and the persisted outcome reader with the old slim shape and verifies a completed lookup without an applied version.

The extended actual producer test at `tests/unit/web/composer/test_service.py:4726` drives discovery, real `set_source`, and cached discovery through the service. It verifies current version 1 → 2 and validation, unchanged discovery data, affected nodes, two discovery invocation records with miss/hit metadata, and one discovery dispatch plus the mutation. Exact provider/audit equality is checked for both discovery calls. Persisted private diagnostic evidence is decoded and compared with the delivered envelope, including `stable_hash(delivered)` as its retained SHA-256.

The four catalog cases at `tests/unit/web/composer/test_compose_loop_audit_wiring.py:1183` exercise real handlers returning Pydantic catalog data, one dispatch across two calls, two successful audit records, exact delivered/audit envelope equality, and retained data. The registry receipt records nine selected cacheable contracts and zero legacy cacheable declarations, with positive/negative controls; omitting an invented legacy public tool is justified.

The malformed-cache controls at `tests/unit/web/composer/test_service.py:4883` retain one successful miss followed by one buffered PLUGIN_CRASH, no malformed-hit provider egress, one handler invocation, and only the miss in durable tool rows. The encoder-failure control remains present. No fallback, handler, cache-key, admission, budget, progress, routing, prompt-guard, persistence-writer, or lifecycle-policy change appears in the patch.

## Code quality assessment

**Sound and appropriately small.** The production edit deletes the competing envelope definition and reuses the existing nominal serializer. It introduces no new abstraction, dynamic attribute probing, suppression, dependency, or permissive parsing. The tests check observable service behavior and persisted evidence rather than only the replacement expression. Existing credential filtering and redacted display hashing remain in their established downstream paths; the larger audit envelope receives those same projections.

Critical findings: none.

Important findings: none.

Minor actionable findings: none. No cosmetic changes are requested.

## Verification evidence reviewed

These are recorded implementation results, not independent reruns:

| Receipt | Observed result |
| --- | --- |
| `red.log` / `red.exit` | Seven intended assertion failures, exit 1; the slim hit payload lacks the delivered envelope fields. |
| `green.log` / `green.exit` | 26 passed, exit 0. |
| `final-tests.log` / `final-tests.exit` | 237 passed, exit 0, including cache/audit, response contracts, historical outcomes, redaction, nominal-schema prompt guard, and attribute contracts. |
| `ruff`, `ruff-format`, `mypy` receipts | Exit 0; three files lint/format clean, changed source type-check clean. |
| `contracts` receipts | Exit 0; contract alignment and soft-mapping census pass. |
| `frozen-gates` receipts | Five attribute-contract tests passed, exit 0. |
| `diff-check.exit`, `freeze.exit` | Exit 0 and successful freeze/checksum verification. |
| `registry.log` / `registry.exit` | Both imports resolve into the candidate roots; selected=9, legacy=0, exit 0. |

The report identifies scoped commands, worktree import roots, local test temporary paths, and final integration limits. The logged red failures are meaningful regressions, not setup failures. The successful focused results and current file hashes support this task-level review.

## Considered but declined

- Adding a fake public legacy cacheable tool: the recorded live registry has no such reachable declaration; manufacturing one would expand scope without proving current behavior.
- Refactoring provider/audit serialization into a new single-pass API: the established owned projections are deterministic and already shared; this fix does not need a broader serialization redesign.
- Changing crash persistence or lifecycle settlement: the existing malformed-hit behavior is preserved and the requested no-egress/no-duplicate controls are covered; altering failure settlement belongs to the separately owned lifecycle work.
- Claiming ordinary full-suite, PostgreSQL, proof, or operator/source-binding clearance: this candidate review was explicitly bounded and authorized no reruns; the parent owns those checks on the final combined candidate.

## Verdict

**Approve this task. No fixes required.** The minimal production change satisfies tracker 11.15 and is backed by focused real-producer, durable-evidence, compatibility, and failure-control evidence. This is task approval, not final integration clearance or tracker closure.
