# Architecture Analysis — Coordination Plan

## Analysis Configuration

- **Scope**: `src/elspeth/` (all Python packages + `web/frontend` React/TS),
  `elspeth-lints/`, `gateway/`, `tests/` (structure only), `evals/`,
  `scripts/` (gates), `deploy/`.
- **Pinned tree**: `release/0.8.1` @ `85ebf2739`, read from detached worktree
  `.claude/worktrees/arch-analysis-pin`. (HEAD moved 780ef0f56 → 85ebf2739 during
  the scan — a sibling session merged S0 strict tool contracts — so all agents
  read the pin.)
- **Deliverables**: Option C — Architect-Ready (01 discovery, 02 catalog,
  03 diagrams, 04 final report, 05 quality assessment, 06 architect handover).
- **Strategy**: PARALLEL (ultracode workflow). 25 subsystem slices + 4 cross-cuts ≫ 5
  subsystems, ~735K LOC total, slices are loosely coupled at the file level.
- **Fan-out budget (user-selected)**: ~20–30 agents; see agent count below.
- **Time constraint**: none stated.
- **Complexity estimate**: HIGH (ultralarge tier).

## Measured size (at pin `85ebf2739`; re-measured after validation, see 01 §2)

The first version of this table was measured at pre-pin `780ef0f56` (web 259,631; total 489,093/880; tests 2,375), and the frontend row counted test code as product code. Both were corrected here, per `temp/validation-01-discovery.md` and critic gap G1 (`temp/validation-completeness-critic.md`).

| Area | Lines | Files |
|---|---:|---:|
| `src/elspeth/` production Python (excl. frontend) | 489,459 | 875 |
| of which `web/` | 259,872 | 360 |
| `web/frontend` TS/TSX, **production** | 69,903 | 259 |
| `web/frontend` TS/TSX, **tests** (`*.test.*`, `*.spec.*`, `tests/`, `test/`, `e2e/`, `__tests__`) | 132,755 | 338 |
| `elspeth-lints/` (Python) | 45,589 | — |
| `gateway/` (Python): `src` 3,230 production · `tests/` 5,757 · conformance/mock/scaffold 1,511 (test & dev tooling) | 10,498 | — |
| `tests/` | — | 2,380 |

## Slice partition (explorer work-list)

| # | Slice | Paths |
|---|---|---|
| S01 | Contracts (leaf) | `contracts/` |
| S02 | Core (non-Landscape) | `core/` minus `landscape/` (config, dag, schema_shape, templates, expression_parser, canonical, payload_store, security, secrets, checkpoint, rate_limit, retention, blobs_inline, llm_*) |
| S03 | Landscape A — DB/schema/composition | `core/landscape/*.py` (schema, database, factory, top-level repositories, journal, exporter, export_*, lineage, reproducibility, model_loaders) |
| S04 | Landscape B — ledgers | `core/landscape/{execution,scheduler,data_flow}/` |
| S05 | Engine A — orchestration | `engine/orchestrator/`, bootstrap, commencement, journal_restore, scheduler_drain, work_items, scheduler_work_codec, dependency_resolver, spans, retry, clock |
| S06 | Engine B — row processing & barriers | processor, executors/, barrier_coordination, coalesce_*, row_union_executor, tokens, token_traversal, dag_navigator, triggers, batch_adapter, aggregation_result |
| S07 | Plugins A — infra, sources, sinks | `plugins/{infrastructure,sources,sinks,llm}/` |
| S08 | Plugins B — transforms | `plugins/transforms/` |
| S09 | Operator surfaces | `cli*.py`, `config_loading.py`, `tui/`, `mcp/`, `composer_mcp/`, `telemetry/`, `testing/` |
| S10 | Composer core loop | `web/composer/` service, state, protocol, pipeline_planner, planner_authoring_aids, tool_batch, no_tool_*, llm_response_parsing, prompts, progress, _compose_loop_carriers |
| S11 | Composer tools | `web/composer/tools/` |
| S12 | Composer advisor/custody/redaction/proposal | remaining `web/composer/*.py` + `skills/` |
| S13 | Guided lane (being retired) | `web/composer/guided/`, sessions `_guided_*`, `guided_*` |
| S14 | Sessions domain | `web/sessions/` minus `routes/` |
| S15 | Sessions HTTP routes | `web/sessions/routes/` |
| S16 | Web execution | `web/execution/` |
| S17 | Web coordination | `web/coordination/` |
| S18 | Web app composition, auth & identity | `web/app.py`, `config.py`, `auth/`, `secrets/`, `middleware/`, `preferences/`, `sso_wiring`, `key_derivation`, `dependencies`, `compartments`, `interpretation_state` |
| S19 | Web supporting domains | `web/{blobs,plugin_policy,catalog,shareable_reviews,audit_readiness}/`, `validation.py`, `provider_config_policy.py`, `landscape_access.py` |
| S20 | Deployment & ops | `web/_aws_ecs_acceptance/`, `_azure_container_apps_acceptance/`, `_acceptance_common/`, `*acceptance*.py`, doctor, readiness, deployment_*, schema_probe, operator_telemetry, external_state_startup, async_workers, process_recovery; `deploy/`, `Dockerfile` |
| S21 | Frontend data layer | `web/frontend/src/{api,stores,hooks,types,contexts,lib,utils,config}` |
| S22 | Frontend chat & tutorial | `components/{chat,tutorial,composer}/` |
| S23 | Frontend workspace & shell | `App.tsx`, all other `components/`, `styles/`, e2e harness |
| S24 | Enforcement architecture | `elspeth-lints/`, `scripts/` gates, `.github/workflows/`, pre-commit |
| S25 | Satellites | `gateway/`, `evals/`, `examples/` |
| X1 | Cross-cut: dependency & layering (measured) | whole `src/elspeth` import graph |
| X2 | Cross-cut: invariants vs ADRs | trust tiers, audit primacy, composer invariants, ADR conformance |
| X3 | Cross-cut: test architecture | `tests/` structure, pyramid, gates |
| X4 | Cross-cut: existing evidence consolidation | 09-23 web review (79), 09-07 web-split analysis, dated reviews, retired code index findings, legacy issue tracker P0/P1 |

X1 is seeded by a mechanical import matrix computed inline (`temp/import-matrix.md`), not by an agent; X2 is split across two agents (ADR-001..024, ADR-025..049).

## Agent count (honest)

The user chose "~20–30 agents". Actual plan after advisor review: 25 slice explorers
(frontend split into 3) + 5 group validators + 1 discovery validator = **31 (Workflow A)**;
cross-cuts X1, X2a, X2b, X3, X4 = **5 (Workflow B)**; synthesis (diagrams, quality,
report, handover) + final validators ≈ **6 (Workflow C)**. Total ≈ **42**, above the
chosen band, driven by splitting the frontend and the ADR check for depth.

**Measured actuals (supersede the estimate above):** A 31 + B 5 + C1 203 + severity tiebreak 9 = **248 agents** before synthesis (C2), ≈ **30.9M subagent tokens** (A 11.70M + B 1.55M + C1 17.03M + tiebreak 0.67M, from each workflow's usage line). The user approved fidelity over efficiency (2026-09-23), which covers the spend; C2 adds ≈ 10.

## Orchestration

Three sequential workflows so results are read between phases:
- **A** `pipeline(groups, explore-5-in-parallel, validate-group)`: no global barrier;
  each group validates as soon as its 5 explorers finish.
- **B** cross-cuts (independent of the catalog, run concurrently with A).
- **C** synthesis: 02 catalog assembled from validated slice files, then 03 diagrams +
  05 quality in parallel, then 04 report + 06 handover, then final validation.

## Execution Log

Times from 20:21 onward come from artefact mtimes where one exists (`stat -c %y`). Entries marked ≈ or "unmeasured" are estimates. The first version of this log carried unmeasured "~" times that ran up to 70 minutes late (critic G13); they are corrected below.

- 2026-09-23 20:16 Created workspace `docs/arch-analysis-2026-09-23-2016/` (git check-ignore: not ignored).
- 2026-09-23 20:18 User selected Option C (Architect-Ready), fan-out ~20–30 agents.
- 2026-09-23 20:25 Holistic scan done; initially 24 slices + 3 cross-cuts, revised after advisor review to 25 slices (S01–S25) + 4 cross-cuts (X1–X4), as in the table above.
- 2026-09-23 ≈20:20 (unmeasured) Advisor review: finish baseline read, measure imports mechanically, write 01 before orchestration, split frontend ×3, S13 = removal blast radius, composer-invariant measurement method, consume existing evidence.
- 2026-09-23 20:21 (import-matrix mtime) Pinned worktree at 85ebf2739; import matrix + SCCs → `temp/import-matrix.md` (control: engine→contracts=340 rows seen; contracts outbound=0).
- 2026-09-23 ≈20:23 (unmeasured; 01's mtime 20:55 reflects later validator/X1 edits) Wrote `01-discovery-findings.md`.
- 2026-09-23 ≈20:25 (unmeasured) Launched Workflow A `wf_6fdb15cc-918` (25 explorers, 5 group validators, 1 discovery validator).
- 2026-09-23 ≈20:45 (last X-file mtime 20:43) Workflow B `wf_fe638a46-f1c` complete (5/5 agents, 0 errors, ~1.55M subagent tokens, 30 min). X1 corrected discovery §8.2 (module graph acyclic; package SCC is a bucketing artefact of vertical-slice packages) — 01 amended in place.
- 2026-09-23 ≈21:12 (last validation-G5 mtime 21:11) Workflow A `wf_6fdb15cc-918` complete (31/31 agents, 0 errors, ~11.7M subagent tokens, 48 min). Verdicts: 4 APPROVED, 21 APPROVED_WITH_CORRECTIONS, 0 NEEDS_REVISION/BLOCK; 411 claims re-checked, 62 wrong (15 %), all corrected in place. 01-discovery APPROVED_WITH_CORRECTIONS (42 checked / 10 fixed).
- 2026-09-23 ≈21:14 (02 mtime 21:59 reflects the later cross-reference insertion) `02-subsystem-catalog.md` assembled mechanically (11,476 lines) from validated slice files; the validation gate for 02 is the five group reports `temp/validation-G*.md`.
- 2026-09-23 ≈21:14 Concern extraction (deterministic parser, `## Concerns` tables only): 385 rows = 16 High / 128 Medium / 241 Low; 0 Critical.
- User direction 2026-09-23: "I want fidelity rather than efficiency in this specific case" → adversarial verification of High+Medium concerns added to Workflow C.
- 2026-09-23 ≈21:14 Advisor review #2: cross-cut findings were missing from the verification set (extractor keyed on severity only) → added 35 cross-cut gap sections as units; dedup before verify; lenses by claim type (defect=reproduce, measurement=re-measure, judgment=verify facts, doc-drift=both halves) + counter-evidence hunter + provenance; 02 header limitation added; `temp/slice-summaries.md` index built; stray `<pin>/gateway/.pytest_cache` (gitignored, created by X3's probe) removed.
- 2026-09-23 21:15 (verify-input.json mtime) Workflow C1 first launch `wf_96f09aad-69e` FAILED at startup (0 agents) — args placeholder bug on my side; relaunched as `wf_d289b80d-135` with units read from `temp/verify-input.json` (179 units = 16 High + 128 Medium slice concerns + 35 cross-cut gap sections).
- 2026-09-23 ≈21:50 Workflow C1 `wf_d289b80d-135` complete (203 agents, 0 errors, ~17.0M subagent tokens, 36 min): 179 units → 148 clusters (0 missing/dup/bogus); 44 tier-A (2 verifiers), 104 tier-B; 1 tiebreak. Final: High 5 · Medium 84 · Low 57 · Refuted 2. 10/14 reported-High clusters downgraded. Provenance: NEW 88, TRACKED 29, REPORTED-AND-TRACKED 16, PREVIOUSLY-REPORTED 15. status_after_pin UNCHANGED ×148 (control: `git log 85ebf2739..release/0.8.1` = 0 commits).
- 2026-09-23 ≈21:52 `temp/verified-concerns.md` + `.json` generated mechanically from structured verdicts (no LLM summarisation) — the sole authority for 05/06.
- 2026-09-23 ≈21:55 Advisor review #3: min-severity-on-split let the primed skeptic decide severity → neutral severity tiebreak `wf_cb3ff6f1-654` (9 agents: 7 SPLIT + K062 single-vote upgrade + K130 re-typed behavioural drift). Final: High 6 (K051, K056, K062, K063, K103, K123) · Medium 84 · Low 56 · Refuted 2 (K010, K045). `verified-concerns.md/.json` regenerated (`scratchpad/gen_verified.py`).
- 2026-09-23 21:55 (baseline-deltas mtime) `temp/baseline-deltas.md` consolidated mechanically (**264** slice delta rows — originally logged as 275 from a miscounting header-row subtraction; corrected per critic G2 + X2a/X2b ADR conformance tables). 02 gained a row→cluster→verified-severity cross-reference.
- 2026-09-23 21:59 (verified-concerns.json mtime) Severity tiebreak `wf_cb3ff6f1-654` integrated; `02` cross-reference inserted.
- 2026-09-23 22:24–22:59 (03/05/06/04 mtimes) Workflow C2 `wf_90866976-8ce` (9 agents, 0 errors, ~2.77M subagent tokens): 03 diagrams (19 mermaid blocks, all render via mmdc) APPROVED_WITH_CORRECTIONS 63/10; 05 quality APPROVED_WITH_CORRECTIONS 67/4 (+1 untraceable); 04 report APPROVED_WITH_CORRECTIONS 72/8; 06 handover APPROVED_WITH_CORRECTIONS 112/10. Completeness critic (23:19): NEEDS_REVISION, warnings only — 18 gaps (1 High, 7 Medium, 10 Low); none changes a High, a refuted cluster or a verified severity.
- 2026-09-23 23:2x Critic G1 (frontend size counted tests: production 69,903 / test 132,755) and G2 (264 not 275) independently re-measured and confirmed; 00/01 corrected. Remediation round R2 planned: verify G3/G4/G8/G9 items, then targeted edits + re-validation of 03–06.
- 2026-09-24 ≈00:15 R2a gap verification `wf_8c2f2711-89f` (37 agents: 1 sweep + 36 neutral verifiers, 0 errors, ~3.93M subagent tokens): 31 confirmed → K149–K182 (8 Medium, 23 Low), 3 refuted (K163 D15, K180 R15, K181 R16), 2 duplicates merged (G4b→K069, R02 variants→K010). Authority now 182 clusters: High 6 · Medium 92 · Low 79 · None 5.
- 2026-09-24 (R2b mtimes) R2b `wf_76c7e2bd-8e2` (9 agents: 4 editors + 4 re-validators + critic-2, 0 errors, ~1.70M subagent tokens). Re-validation: 03 148/1, 05 46/1, 04 35/3, 06 52/5 (checked/wrong), all APPROVED_WITH_CORRECTIONS, gaps_open [] on 03/04/05. Critic-2 (`temp/validation-completeness-critic-2.md`): G1–G18 → 17 CLOSED, 1 PARTIAL (G13, this log), 0 OPEN; 7 new Low gaps N1–N7.
- 2026-09-24 N1–N7 fixed directly (single-line edits): N1 gateway = `src` 3,230 / `tests/` 5,757 / conformance+mock+scaffold 1,511 (measured; sums to 10,498) in 00/01/03/04/06; N2 02 S22/S23 delta rows annotated with the 69,903 production split; N3 04 D1–D15; N4 02 S10-C14 and S22-C15 R16 annotated refuted (K181); N5 catalog validation = **369 checked / 52 wrong (14 %)** for the 25 entries, 411/62 only with 01-discovery (measured from the A journal) in 02/04/05; N6 05 disambiguates R2 sweep item D15 from 06's decision D15; N7 `e69498f6c` (landed on release/0.8.1 after C1) noted in 04 and verified-concerns — critic-2 checked it touches no cited code. G13 closed by these two entries.

## Totals (measured from workflow usage lines)

| Workflow | Run id | Agents | Subagent tokens |
|---|---|---:|---:|
| A explore + group validate | wf_6fdb15cc-918 | 31 | 11.70M |
| B cross-cuts | wf_fe638a46-f1c | 5 | 1.55M |
| C1 verify (first launch failed, 0 agents) | wf_96f09aad-69e / wf_d289b80d-135 | 0 + 203 | 17.03M |
| Severity tiebreak | wf_cb3ff6f1-654 | 9 | 0.67M |
| C2 synthesis + validate + critic | wf_90866976-8ce | 9 | 2.77M |
| R2a gap verification | wf_8c2f2711-89f | 37 | 3.93M |
| R2b remediation + re-validate + critic-2 | wf_76c7e2bd-8e2 | 9 | 1.70M |
| **Total** | | **303** | **≈39.35M** |
- 2026-09-24 Absolute user-home paths (44 hits in 14 files, AGENTS.md forbids them in tracked files) replaced mechanically with `<pin>` (= `.claude/worktrees/arch-analysis-pin`), `<repo>`, `<session-dir>` and `<scratch>` tokens.
- 2026-09-24 Final consistency sweep after the direct edits: regression grep for `411 catalog` / `275` as a count / `D1–D14` / `3,871 production` / unsplit `202,658` → clean, apart from correction-log history rows; 01:196 and the 02 S21 baseline row annotated; 02 header now lists the appended sections and the 5 annotated rows, and states that the `temp/slice-*.md` copies are unannotated; `verified-concerns.md` discloses the unreconciled K027/K028 disagreement. Severity check (`scratchpad/sevchk.py`, from critic-2): 441 mentions, 1 mismatch, which is 06's VR8 correction-log row quoting the old text (expected). **Analysis complete.**
- 2026-09-24 Post-analysis: the six Highs were fixed by another agent and merged at `bc0f251c4` (plus follow-ups to `ffd704d1a`). Fix review: K103, K123 and K051 reviewed directly (`temp/fix-review-small.md`), all CLOSED with mutation proof. K063 red-team CLOSED (`temp/fix-review-K063.md`, 1 Low: non-scalar group keys bucketed in 6 plugins). K062 and K056 red-team pending. **D1 ruled by the maintainer: keep replay/verify (wired)**, with his own bug list against it.
- 2026-09-25 The detached pin worktree had been removed by a cleanup run outside this session: it was classified IGNORED because of a stray `.benchmarks/`. It was recreated at the maintainer's request as branch **`keep/arch-analysis-85ebf2739`**, checked out at the same path `.claude/worktrees/arch-analysis-pin` (so `<pin>` references still resolve), and **locked** (`git worktree lock`, reason "KEEP: reproducibility anchor…"). `scripts/worktree-cleanup.sh` now classifies it LOCKED even with `--discard-ignored --delete-branches`. The example branch `examples/replay-verify-20260924` was deleted after its merge (`ee96258ca`), and its worktree was removed.
