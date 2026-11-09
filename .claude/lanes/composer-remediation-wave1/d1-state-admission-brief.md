# D1 pure-state admission and proposal budgets (plan T1 + cap portion of T3)

Worktree: /workspace/elspeth/.claude/worktrees/composer-remediation-wave1. Parent supplies current base after preceding legacy task. Read AGENTS.md, CONTRIBUTING whole-tree gates. No agents/commits/broad tests.
User ruling: return known refusal/no-op directly; stage only existing validation-only-admitted changes. Every changed behavior must have tests. Existing server/provider/custody authority remains unchanged. Read d1-implementation-plan.md scope section and T1/T3 only; d1-design-review.md is supporting evidence.

Own src/elspeth/web/composer/tool_batch.py and narrowly necessary internal response/progress contract plumbing, plus integration test_freeform_proposal_prevalidation.py, unit test_proposal_cap_turn_counts.py, and focused service/anchor tests. Do not edit preceding legacy route/UI files.

## Admission behavior

Ordinary incremental graph and state-only wire_secret_ref/wire_blob_inline_ref calls must be previewed once with actual complete ToolContext. Use effect/registry authority, not a hand-maintained name list that silently excludes marker tools. Do NOT preview create_blob/update_blob/delete_blob via execute_tool (durable effects); create_blob remains immediate, update/delete remain staged until following shared non-writing-admission task.

Cache actual expected ToolArgumentError and reuse canonical audited dispatch for feedback (once, no redispatch). Refused result success=False returns directly, no proposal. Successful semantic no-op returns unchanged success, no proposal. Successful changed local repair can stage despite unrelated current/candidate authoring errors; preserve existing ordinary partial-draft policy. Canonical set_pipeline remains subject to existing candidate.acceptable global authored validation, with finalized authority equality rejection before custody/publication. Do not add universal runtime/Stage2 checks. Immediate auto-mode mutations retain existing behavior; this task changes explicit approval admission only. Planner canonical publisher handled separately.

Use immutable/narrow explicit internal admission disposition distinguishing refused vs successful no-change from unapplied results. Existing canonical rejected-candidate result may carry successTrue; do not infer disposition solely from success. Keep existing PREVALIDATION_REJECTED semantics for refused canonical candidate, and give successful no-op truthful no-change feedback rather than relabeling it a failure. Preserve first actual outcome/argument exception; no catch-all swallowing.

Finalize required controls and interpretation/review reconciliation under existing conditions before composition_content_hash comparison. Do not compare version or redacted projection. Meaningful review/provenance/source/branch ordering changes remain changes. Preserve current loop state/validation/runtime preflight for all unapplied responses. No-op feedback must carry original state/version, empty affected_nodes, no validation_delta, and no applied_component echo or claim of graph progress. It must neither fail nor clear anti-anchor progress. Refusal continues appropriate failure accounting. Keep actual model-visible result, audit payload/version, tool outcome, progress copy and repair/no-tool budget behavior consistent.

## Both proposal budget checks

Remove/relocate whole-batch requested-mutation guard near1042 and per-call guard near1811: pending-proposal cap applies only after semantic admission, before inline custody reservation or publication. Refusals/no-ops may return when cap is already full or when batch contains more than cap such calls. Next real stageable change hits existing cap semantics (sequential first-N staging is truthful current architecture), no excess proposal or inline custody resource. Preserve independent tool-call cap, attempted-call accounting, provider call/result IDs, original failed_turn evidence and no fake outcomes for calls not attempted. No store-admission rework here.

## Required behavioral pins

Real compose loop/provider harness, both dialects where pairing differs:
- invalid args and actual handler refusal -> one feedback, zeroproposal, next provider repair stages exactly one; original state baseline retained;
- metadata empty/same-value and representative source/node/output/edge no-ops, changed counterpart, including version-incrementing no-op;
- invalid current draft+successful no-op preserves standing diagnostics/preflight; invalid draft+local successful repair still stageable;
- required controls/review finalization or provenance changes not discarded as no-op;
- exact permitted marker and near-match denied secret policy, blob metadata prerequisites; handler invocation once and no secret value resolution during admission;
- canonical identical public and owned authority no staging; changed authority stages; inline custody semantics no text-only deduplication;
- >cap all-refusal/all-no-op, atcap followedby refusal/no-op, cap+1 actualchange, canonical inline atcap produces no custody reservation;
- anti-anchor/no-tool/progress and audit outcome cardinality; successful no-op no PREVALIDATION_REJECTED/no false applied echo/delta;
- immediate auto-mode counterpart unchanged.

Avoid tests mirroring helper internals; use small parameterized meaningful end-to-end controls plus existing broad fixtures. Prove pre-fix failures. No unrelated baseline fixes/suppressions. If marker behavior requires shared resource extraction, report narrow dependency rather than running destructive preview.

Run focused integration+cap+affectedservice tests; response/envelope/progress gates relevant to changed status fields; contracts and scoped Ruff/mypy. Both worktree source roots and explicit provenance; logs and exit files in shared lane. Parent owns full/static/corpus/PostgreSQL gates after combined D1. Freeze and report d1-state-admission-report.md + d1-state-admission-review.diff (including new files); include semantic changes, red/green evidence and pending store/canonical publisher scope.
