# Queue scope admission — independent task review

Reviewed 2026-11-09 against the accepted queue-admission plan and design review. Scope is SG-07 / tracker 08.01, not D29 or the parent's combined release gate.

## Verdicts

**Specification: PASS.** The candidate implements the accepted preservation fix and its route-specific proof obligations. Every admitted non-null scope field now reaches the existing intrinsic queue guard; canonical omission/null and incomplete-graph behavior remain unchanged.

**Quality: PASS.** No actionable correctness, regression, maintainability or test-integrity findings identified in this bounded patch. The production change is exactly three explicit constructor keywords; it adds neither a second authority nor a new policy. Recommend parent integration subject to its already-assigned combined gates. This review does not close the tracker.

## Reviewed identity and scope

Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-queue-admission`.

Live HEAD measured as `1bc91710fbbdd205376f71dadf8aa3ca557229d8`; live status contains exactly the five reported modified/new files. Full patch SHA-256 measured as `37f322be46b8b4ee00d73509578aea8e4023362c225747251e8bafd9954c707d`. All five live file hashes match both frozen manifests. Reverse apply check succeeded against the live candidate. Local AGENTS.md and CONTRIBUTING.md match the main copies; relevant whole-tree, trust-domain, fixture, Composer and gate instructions were consulted.

Read the complete review patch, plan/design/implementation reports, owning constructor/handler/model/guard, shared refusal assertion, complete-graph fixture, and current MCP, freeform-preview and REST replay code. The production hunk adds only `scope_name`, `scope_opener`, and `scope_policy` at `tools/transforms.py:632–634`. The existing guard still uses `is not None`, sorts field names and runs before `with_node`. Existing early return, refusal helper, D2 checks, schemas and bulk builder are untouched. No adjacent reconciliation, sink-mirror, planner or migration behavior is introduced.

## Specification evidence

- Core tests exercise the real registered handler and schema-enabled dispatcher, with and without an existing queue seeded through real dispatch. The table includes all nonempty subsets, both valid policies, individual mixed-null discriminators and empty name/opener strings. Exact messages exclude null fields. Shared assertions pin refusal identity, standing findings, version, empty affected nodes, absent data/prior validation/delta; added assertions pin applied-component absence and complete private snapshots/content hashes.
- Canonical tests cover omitted/default options, all-null scope, both description locations, changed replacement, unset structural fields and success with incomplete telemetry.
- Complete bulk/incremental controls use real catalog/policy, actual session-owned source files, source/queue/consumer/output topology, and first prove the exact canonical payload is valid through both routes. Bulk retains its broader arbitrary-policy transport boundary and candidate-only refusal envelope.
- MCP tests call the actual SDK handler. Transparent dispatch observation captures private state before outbound redaction; a subsequent real read observes reconstructed private equality/version/hash and checkout authority. Each call is audited once. Refusal remains ordinary semantic text; malformed input is ARG_ERROR before dispatch. Saved bytes/inode/mtime remain intact, and a separate canonical edit plus explicit save/load proves continued persistence authority.
- Freeform tests execute the real approval preview with a scripted provider and established CLEAN advisor fixture. Changed description is a genuine positive staging control. Individual/all-field forbidden edits produce no proposal or composition change. REST tests replay real legacy pending proposals, observe intrinsic refusal and exact subject/message, preserve the private composition/current row, terminalize rejection, and verify repeated acceptance conflicts without redispatch. Required proposal/chat/audit writes are allowed.
- Malformed direct/model, schema-return, schema-raise, MCP and legacy replay channels remain distinguished. Tests do not manufacture planner incremental exposure or decide graph-bound review reconciliation.

## Verification evidence and limits

No duplicate pytest suite, broad scan, provider call, source edit, commit or additional agent was launched for this review. Results below are inspected implementer receipts, not newly executed test claims:

| Preserved receipt | Exit and result |
| --- | --- |
| Corrected `all-red-final` | 1; 89 failed, 33 passed |
| Final `all-green` | 0; 122 passed |
| `affected-contracts` | 0; 499 passed |
| Each of three field-omission mutants | 1; its own single-field public-dispatch regression failed on `success=True` |
| Ruff, format, mypy, contracts | Each 0 |
| Freeze check and patch check | Each 0 |

Inspected corrected red error records establish 64 core silent admissions, 16 complete-route incremental admissions, four incorrectly staged approvals, one SDK admission and four REST HTTP-200 commits. The 33 passing controls and retained earlier harness-failure logs distinguish the corrected product regression from missing fixture fields, custody setup and mistaken error-category expectations. Mutant receipts independently exercise each field; final live hashes and restored affected regressions corroborate restoration.

Provenance receipt resolves both Python packages into the task worktree. Independently compared the actual keyless baseline/final JSON finding multisets, removing only line/column positions: 5,224 each, zero additions/removals; stderr is byte-identical. Independently controlled that comparator with a distinct-message positive and position-only negative. Both lint process exits are 1, consistent with the standing corpus: this is no-new-finding evidence, not signed clearance or a clean corpus.

The historical unchanged-source red setup and individual mutation edits are supported by the supplied execution report and retained logs, not independently replayed in this review. Parent frozen-tree full/official partitions, whole-source masquerade/live-tree proofs and applicable operator verification remain integration obligations, not defects in this task implementation. No schema, SQL, persistence implementation or locking change creates a new task-local PostgreSQL obligation.
