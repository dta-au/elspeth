# D1 state admission and proposal budgets

Status: implemented, staged, frozen for parent review. No commit. Scoped behavioral and final static checks pass. The response/envelope gate group remains red on the same 14 pre-D1 scanner failures; it is not a green whole-tree gate.

Worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`.
Base and final HEAD: `66739807c9acbdae6f2d7d9f1de858b8e69c9793`.
Review diff: `d1-state-admission-review.diff` (all five staged paths; no new repository files).
Diff SHA256: `91379cf76399ac5d38de03c6d6c27c334c666129dc3f8b50ab45cf9f7b1fa8ac`.
Scope: `tool_batch.py`, integration prevalidation tests, proposal-cap turn-count tests, cap rejection audit tests, and the single affected envelope structural assertion plus four negative controls.

## Implemented semantics

- Explicit approval previews actual ordinary pure-state handlers once with the existing complete dispatch context. Registry effect authority excludes durable blob-store effects and includes both secret/blob marker tools. `create_blob` stays immediate; update/delete remain staged without executing their durable handlers.
- The first actual result or exception is retained. Argument errors are re-raised once inside canonical audited dispatch instead of swallowed and staged. Refusals return directly without proposals. Successful finalized content-equal results return success without proposals.
- `_UnappliedAdmission` is frozen and explicitly distinguishes refusal from successful no-change. Canonical globally rejected candidates may retain `success=True`; their refusal disposition still drives failure accounting and truthful blocker progress. Existing `PREVALIDATION_REJECTED` payload semantics remain canonical-refusal-only.
- No-op feedback uses the original state/version, standing validation and runtime preflight, empty affected nodes, no validation delta or applied-component echo. Existing `post_call_hints` carries an explicit unchanged/no-proposal message. Completion progress says unchanged. No-op neither clears nor increments anti-anchor state or earns graph-mutation progress.
- Current loop state, validation, and runtime preflight remain unchanged for all unapplied responses. Real refused-candidate diagnostics remain available for repair. Successful changed local repair remains stageable despite unrelated draft authoring errors; no new global-green or runtime/Stage2 gate was added.
- Required controls/review reconciliation occur under existing conditions before `composition_content_hash` comparison. Required controls can therefore turn an equal metadata request into a real owned-state proposal. Source ordering remains meaningful. Equal inline text with a new blob identity/provenance is not deduplicated.
- Complete canonical candidates keep their existing `acceptable` authored-validation gate. Explicit-approval public singleton/named-source equal candidates return successful unchanged feedback before custody/publication. Raw owned-state authority is not admitted through the provider schema; real incremental finalization produces owned-state authority through its existing trusted seam.
- Immediate ordinary auto-mode metadata calls retain their existing version increment, including semantic no-ops.

## Proposal cap and audit boundary

Both requested-mutation guards were removed/relocated. The per-turn cap now counts proposals after semantic admission, before publication and before inline custody preparation/reservation. Batches of 11 refusals, argument errors, or successful no-ops are delivered; refusal/no-op feedback is also delivered after ten proposals. A real cap+1 change raises the existing bounded convergence error after the first ten proposals have been staged.

The cap+1 call **is semantically observed**: its pure preview must run to distinguish a change from a no-op. It is then denied before entering the staged/audited dispatch path. It creates no proposal, inline custody resource, tool outcome, synthetic result, or fake post-version. The remaining suffix is not observed. This preserves the existing convergence-error budget surface instead of inventing a dispatched tool refusal for a call which was never admitted to dispatch. The carrying provider completion remains attached to the exception.

The convergence carrier now includes only the real current-batch invocation prefix. Earlier persisted discovery invocations are excluded from that suffix. Original `ctx.failed_turn` and prior-turn counters remain unchanged; the turn-count test pins their assistant identity and attempted/persisted counts. Exact current-prefix tool-call IDs and absence of a denied-call invocation are pinned. No tool-call input cap or provider pairing rule was changed.

Parent explicitly approved the current-batch suffix boundary and preserving the ToolResult wire schema (no new public no-change enum/status). No new lifecycle status, schema, migration, provider permission, or custody authority was introduced.

## Behavioral evidence

The real compose-loop/provider/session harness pins:

- Both dialects: actual argument error/refusal/empty or same-value no-op followed by exactly one repair proposal; one feedback per actual call, handler/execute observation once, original state baseline retained.
- Metadata/source/node/output/edge/full-replacement no-ops and changed counterparts; source/node/output same-value and absent-key-null patches; version-incrementing equal mutations.
- Invalid current draft: successful no-op retains diagnostics/preflight by identity, while useful changed metadata repair remains stageable.
- Required-control/review finalization is not lost to equality, source order remains significant, and same literal inline data with different blob authority stages another real proposal.
- Exact secret allowlist positive and near-match denial, same-marker no-op, no secret resolution; ready/missing/nonready/LLM-modality blob metadata and same-marker behavior; durable update/delete never execute during preview.
- Greater-than-cap refusal/argument/no-op batches, at-cap refusal/no-op, cap+1 real change, inline cap+1 without custody allocation, exact earlier-turn evidence and audited-prefix IDs.
- Anti-anchor no-op neutrality, no false delta/echo/progress, canonical success-true refusal blocker progress, ordinary immediate auto version behavior.

Historical pins intentionally updated:

1. Invalid incremental metadata used to expect a pending proposal. It now expects one `ToolArgumentError`, zero proposals.
2. Cap audit tests used to expect no current-batch invocation. They now require the ten admitted, unapplied proposal invocations and no denied call.
3. The envelope structural assertion follows the real nominal admission helper and the canonical refused constructor seam. Four AST mutation controls remove each required change field and prove the assertion fails. The generic walker, inventory, generator, and fence policy were not broadened.

## Red evidence and fixture corrections

`d1-state-red2.log/.exit`: exit 1, initial pre-fix run 17 failed/1 passed. Two canonical cases initially had malformed fixture deserialization, so those are not claimed as valid red evidence.

The final fixture selection was then run against immutable base tool-batch source using `d1-state-base-oracle.py`, which replaces only the module in memory with `git show 66739807:src/elspeth/web/composer/tool_batch.py`. No product checkout mutation was used. Both source roots and the exact selected base are printed in the log.

`d1-state-base-red.log/.exit`: exit 1, **37 failed, 9 passed, 24 deselected**. Failures catch refusal/argument/no-op admission, both cap sites, canonical equality, mutation-family equal edits, and marker preview. Passing controls include real changed edits, unchanged immediate auto behavior, and required-control finalization making an equal request stageable.

`d1-state-refused-progress-red.log/.exit`: exit 1, one actual canonical profile-refusal test fails because a `success=True` refused candidate has no blocker completion. The final one-line disposition-aware completion change makes the same producer pass in the final 112-test run.

Fixture corrections were confined to test inputs: metadata's actual default name; deserialized output `name` versus public `sink_name`; metadata description; valid public named-source input rather than raw owned authority; a permitted output format change rather than forbidden append mode; and actual Composer blob creation for LLM modality rather than treating a creator label as modality. No product policy was relaxed to accommodate these fixtures.

## Commands, provenance, and terminal exits

Every Python test/check used explicit worktree cwd, the worktree `.venv/bin/python`, and exported/prefixed:

```
PYTHONPATH=/workspace/elspeth/.claude/worktrees/composer-remediation-wave1/src:/workspace/elspeth/.claude/worktrees/composer-remediation-wave1/elspeth-lints/src
```

Every pytest run additionally used `-n0 -o pythonpath='/workspace/elspeth/.claude/worktrees/composer-remediation-wave1/src /workspace/elspeth/.claude/worktrees/composer-remediation-wave1/elspeth-lints/src'`.
`d1-state-provenance.log` proves both imported packages resolve into this worktree.

| Artifact stem (`.log` + `.exit`) | Terminal result |
| --- | --- |
| `d1-state-focused` | exit 0: 153 passed; integration prevalidation, caps, carrying LLM-call audit, freeform required-controls integration, secret collector, blob-inline tools |
| `d1-state-service` | exit 0: 50 passed, 220 deselected; explicit approval, failed-mutation budgets, final preflight, empty-state/no-tool finalization, forced repair |
| `d1-state-contract-gates` | exit 1: initial 413 cases, 398 passed/15 failed; 14 pre-D1 scanner failures plus the intended old structural pin |
| `d1-state-contract-gates-final` | exit 1: 417 cases, 403 passed/14 pre-D1 failures, including the repaired structural assertion and four passing negative controls; before the final one-line refused-progress correction |
| `d1-state-final-refused-progress-green` | **exit 0: 112 passed on final frozen tree**; all 72 integration prevalidation cases, progress suite, carrying-call audit, cap turn counts, structural assertion, four negative controls |
| `d1-state-ruff-final` | exit 0: all five changed paths |
| `d1-state-format-final` | exit 0: all five paths already formatted |
| `d1-state-mypy-final` | exit 0: `src/elspeth/web/composer/tool_batch.py` |
| `d1-state-contracts-final` | exit 0: `python -m scripts.check_contracts`; census unchanged, 2615 soft occurrences, 60 boundary-parsed |

The response group includes compose-loop anti-anchor, tracker, progress, ToolResult envelope, envelope gate, compose-loop envelope, dispatch-arm characterization, no-tool response, response redaction, and composer/session attribute contracts. The service/response selections each emitted one existing upstream Pydantic `ReadOnly` warning; no warning suppression was added.

## Baseline gate limitation (not a green gate)

All 14 remaining response-gate failures stop at `tools/generation.py:2215`, conditional `**` splat of an `IfExp` involving `matched_code`, unsupported by the envelope gate's `_dict_literal_keys` attribution walker. Parent traced this to prior-remediation commit `f598aa43de`; it is a pre-D1 scanner regression, not an upstream dependency fault or a D1 introduced failure.

`d1-state-envelope-baseline14.log/.exit`: **exit 1, the same 14 named failures**, with original-base tool-batch runtime and AST reads. Other paths remain unchanged. `d1-state-envelope-failure-comparison.txt` asserts the initial set minus the one repaired structural pin equals both the 14-failure original-source set and the final 14-failure set. Original-source control `d1-state-envelope-baseline.log/.exit` reports exit 1, one scanner failure plus the old structural assertion passing.

Base and worktree `generation.py` Git blob IDs are identical: `c36c852c37510493d2f1bce6cc3441f8b5743f15`. No scanner/generator baseline repair or suppression was included. Parent owns its separate repair. The full 417-case response run must continue to be reported as **14 baseline failures**, not green.

## Frozen handoff and remaining scope

`d1-state-frozen-final-before.sha256` and `d1-state-frozen-final-after.sha256` match for all five changed paths across the final 112-case run and final static checks. HEAD remains the exact base. `git diff --check` and staged diff check passed. Exactly five intended files are staged; deliberate untracked frontend `node_modules` symlink is preserved and excluded. No legacy route/UI files were changed, no agents spawned, no commit, full suite, PostgreSQL suite, or trust-tier/static corpus run was performed by this task.

Remaining separately owned D1 work: shared non-writing update/delete blob prerequisites and quota admission; authoritative proposal-create race handling; canonical/planner auto-mode/no-change publication and existing pending canonical acceptance terminalization. Immediate ordinary auto mutation behavior remains unchanged. Parent owns independent review and combined full/static/corpus/PostgreSQL verification after those tranches; this report does not claim that integrated D1 or whole-tree release gates are complete.
