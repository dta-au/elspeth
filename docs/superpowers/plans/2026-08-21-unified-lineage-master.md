# Unified Group Lineage & Barrier Scopes — Master Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this campaign plan-by-plan, task-by-task. This master file is the sequencer; each workstream plan is self-contained.

**Goal:** Implement spec rev 3.2 — one lineage and settlement system (`lineage_path` of
typed frames) across fork/coalesce/row_union and barrier scopes, retiring the tri-field,
before launch locks the audit model in.

**Spec:** `docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md`
(rev 3.2, all rulings 1–28 final, vocabulary signed off 2026-08-21, synthesis
corrections applied 2026-08-22).

**Provenance:** plans drafted 2026-08-21/22 by a 14-agent scout/draft/review workflow,
fixed under the synthesis decision canon, cross-plan-verified (two rounds; final
residuals applied 2026-08-22). Review verdicts: 4× fix-then-approve → fixes applied.

## The plan set (execution order)

| # | Plan file | Scope | Gate to start |
|---|---|---|---|
| 0 | `2026-08-21-unified-lineage-ws0-corrections.md` | False-docstring/comment corrections; standalone value | none |
| P | `2026-08-21-unified-lineage-protocols.md` | Standing procedures §S1–§S5 + oracle-freeze machinery (Tasks 1–2), freeze execution (Task 3), casualty worklist, wiring verification | Tasks 1–2 anytime; Task 3 requires WS1a Task 8a (nested fixtures) |
| 1a | `2026-08-21-unified-lineage-ws1a-model-core.md` | LineageFrame/lineage_path beside stored fields (prep slices), GroupLossSpec, three new tables (epoch 34), journal plumbing, durable writers, empty-expansion mint, nested fixtures (Task 8a), join carriers, join_group_id off TokenInfo | WS0 done |
| 1b | `2026-08-21-unified-lineage-ws1b-flip-replay-checkpoint.md` | Path-aware consumer rewrites (Phase A), THE ATOMIC FLIP (Phase B: four-table retirement, epoch 35, replay predicates, codec, fixture regeneration), AST guard, **WS1 CHECKPOINT** (Phase C) | WS1a done; protocols Task 3 freeze exists before Phase B (Task 7) |
| — | **WS1 CHECKPOINT (STOP gate)** | Full suite green with deltas only in the §4.1a-enumerated surfaces; frozen-oracle diff clean; trust-tier corpus delta zero; wardline green. **If unreachable: STOP, surface to maintainer. Do not proceed.** | — |
| 2 | `2026-08-21-unified-lineage-ws2-config-validation.md` | collectors:/scopes: config, binding registry, all §7 rules (whole-roster, SESE, r28, r25, depth cap 5, fixpoint-bound derivation), canonical-hash pin corpus (Task 1, BEFORE code changes), casualty migrations in-slice, composer parity + three-pin | WS1 checkpoint passed |
| 3 ∥ 4 | `…ws3-settlement.md` ∥ `…ws4-collector.md` | WS3: settle-member seam, frame guard (both contexts), group_losses ledger, escalation, depth-5 unwrap, mutants. WS4: collector executor, pending-state re-keying, always-journal pin. Skeletons parallel; WS4 Tasks 11–12 gate on WS3. | WS2 done |
| I | WS3+WS4 integration | Own line item: processor wiring, e2e families, multi-worker tests, the `PipelineConfig collector_settings=/scope_settings=` reconciliation points marked inline in WS4/WS5-6 | WS3 + WS4 done |
| 5+6 | `2026-08-21-unified-lineage-ws5-ws6-resume-observability.md` | Satisfiability gate both surfaces, collector death-matrix + depth-5 crash+resume, disposition vocabulary, group MCP surfaces, depth-3 forensics acceptance, ADRs + rolling docs | Integration done |

**Scout inventories** (inputs, keep until campaign close):
`2026-08-21-unified-lineage-inputs/{consumer-roster,fixture-oracle,test-harness}.md`

## Global constraints (bind every plan)

- Standing procedures: `2026-08-21-unified-lineage-protocols.md` §S1–§S5 govern fixture
  freezing, per-slice gates (full `pytest tests/`, trust-tier corpus COUNT diff —
  add nothing, wardline gate), casualty retirement, judge-bundle sequencing (no staging
  across the campaign; operator signs after churn settles), and the WS1 STOP rule.
- Shared checkout: stage by explicit pathspec only; never stage
  `src/elspeth/web/composer/state.py` / `tests/unit/web/composer/test_state.py` unless
  the task owns them (WS2 modifies state.py by symbol anchor at execution time).
- Mechanical pre-flight (WS1b, WS3, WS4): before starting, grep every "WS1a Task N" /
  sibling-plan citation and diff the named artifact against that task's Produces block.
- Mutation tasks run with `-n 0`. No calendar commitments.

## Decision canon (ratified 2026-08-22 synthesis — spec-consistent; maintainer may veto)

Shared helpers `path_branch_name`/`path_fork_group_id`/`path_expand_group_id` +
`pop_closer_frame` live in `contracts/identity.py` (WS1a Task 1). `CloserKind` is a
StrEnum. `GroupBindingRegistry.binding_for(frame)` → `GroupBinding` (`member_roster`).
One fixpoint formula: `derive_escalation_fixpoint_bound(depth) = 1000 + 8·depth`, owned
by WS2, on the graph accessor pair. One `CollectorSettings`/`ScopeSettings` copy (WS2):
`input`/`on_success` required, `on_error` optional-None, `on_group_failure` default
`quarantine`. `group_records` mints both kinds; FORK roster authority stays config.
`token_work_items.join_group_id` kept; three new tables enter the portable export at
the flip. Outermost quarantine ⇒ COMPLETED-family run status. Escalation reason token
`group_failed`, token_id = opener. Collector M-output release = fresh EXPAND group.
Collector BLOCKED `barrier_key = "collector:<name>:<group_id>"` (stated contract).
Freeze compare suite deleted at campaign close. ADR-038 sweep does not mirror the
satisfiability refusal. MCP keeps path-derived legacy wire names (ruling 21);
`TokenRecord.lineage_path` = LineageFrameEntry dicts.

## Open items

1. ~~Screened-example pedagogy~~ **RULED 2026-08-22 (joint arch+systems advice,
   maintainer approved): TWO variants** — `settings_screened.yaml` becomes
   screen-before-fork (source-known `baseline_quality` predicate; SUCCESS run; sink
   kept), plus new `settings_screened_at_settlement.yaml` demonstrating screen-as-loss
   on the computed `score` through the settlement channel (PARTIAL by design; honest
   cost README). Implemented in WS2 Task 7 Step 5b; full ruling in protocols RC-4.
   **No open decisions remain.**
2. Execution-time reconciliation (not decisions): the WS3+WS4 integration item's
   builder/PipelineConfig spellings — marked inline in WS4/WS5-6.
