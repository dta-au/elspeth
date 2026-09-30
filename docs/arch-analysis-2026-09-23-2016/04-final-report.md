# 04 — Final Report: ELSPETH architecture as built

**Pinned tree:** `release/0.8.1` @ `85ebf2739`, read from the detached worktree `.claude/worktrees/arch-analysis-pin` [measured: git -C <pin> rev-parse --short HEAD → 85ebf2739; git -C <pin> status --short | wc -l → 0].
**Baseline:** `ARCHITECTURE.md` at the pin (last updated 2026-09-11) and the ADR set under `docs/architecture/adr/` [measured: grep -n 'Last Updated' <pin>/ARCHITECTURE.md → line 5, 2026-09-11].
**Date:** 2026-09-23. **Status of this document:** the synthesised as-built record of the analysis. It adds no new review and re-rates no concern; every severity is the final severity in `temp/verified-concerns.md` [VC §Method][00 §Execution Log].

**How to read the source tags.** Every factual claim carries at least one tag.

- `[K0NN]` is a verified concern cluster in `temp/verified-concerns.md` (+ `.json`), the sole authority for severity and wording. Wording here follows the cluster's *verified claim*, not its title and not the slice row it came from [VC §Method]. Five clusters were refuted and appear only as refuted: K010 and K045 in C1, and K163, K180 and K181 in the R2 gap round [VC §Outcome]. K149–K182 come from that R2 round [VC §Method].
- `[critic Gn]` / `[critic §N]` is gap Gn, or section §N, of `temp/validation-completeness-critic.md`, the completeness critic's report that drove the R2 revision (see the section at the end).
- `[S07 §Section]` is a section of slice S07's entry in `02-subsystem-catalog.md`. `§header` is the entry's Location and Measured-size lines, `§Responsibility` its Responsibility line, `§Complexity` its "Complexity & tech-debt hotspots" section; other names are the entry's own headings.
- `[X1 §2.7]`, `[X2a §2]`, `[X2b §B3]`, `[X3 §1.1]`, `[X4 §E]` are sections of the cross-cut files in `temp/`.
- `[01 §N]` is `01-discovery-findings.md`; `[00 §…]` is `00-coordination.md`; `[VC §Method]` / `[VC §Outcome]` are header sections of `temp/verified-concerns.md`.
- `[03 §N]`, `[05 §N]`, `[06 §N]` point to the diagram set, the quality assessment and the architect handover. Where this report summarises one of them, the primary tag is carried alongside.
- `[ruling N]` is maintainer standing ruling N from the report brief (2026-09), numbered as in 06: (1) architecture complete; (2) guided mode retired, tutorial kept on the shared backend as the ADR-031 canary; (3) no tech debt, no legacy pathways, no old rows; (4) unwired intent needs a wire-or-remove decision; (5) plugins are closed interfaces; (6) more developers are incoming; (7) composer invariants, with K023 as a sanctioned carve-out; (8) the trust-tier CI red is deliberate [06 §Source tags].
- `[measured: …]` is a command this report ran itself at the pin.

---

## 1. Executive summary

**What ELSPETH is.** An auditable Sense/Decide/Act pipeline engine: a pipeline is a DAG of sources, transforms, gates and barriers, and sinks, and every row, token, node state, external call, routing decision and sink effect is recorded in the Landscape audit database [01 §1]. Two authoring surfaces, version-controlled YAML run by the `elspeth` CLI and the authenticated Web Composer (an LLM tool loop over FastAPI + React), share one runtime: plugin contracts, graph validation, executor, Landscape and run accounting [01 §1].

**How big it is.** Production Python is 489,459 lines in 875 files [measured: find src/elspeth -name '*.py' -not -path '*/__pycache__/*' -not -path '*/frontend/*' | xargs cat | wc -l → 489459; | wc -l → 875]. `web/` is 259,872 of those lines, 53 % [measured: same instrument over src/elspeth/web → 259872][01 §2]. The React/TypeScript frontend adds 69,903 lines of production TS/TSX in 259 files, plus 132,755 lines of frontend tests in 338 files; the 202,658-line total counts both [measured: git ls-files 'src/elspeth/web/frontend/*.ts' 'src/elspeth/web/frontend/*.tsx' → 597 files / 202,658 lines; split by `.test.`/`.spec.` suffix or a `tests?/`, `__tests__/` or `e2e/` directory → production 259 / 69,903, test 338 / 132,755; control: `App.tsx` lands in production, `App.test.tsx` in test][critic G1]. Around it sit a 45,589-line custom lint engine, a standalone LLM gateway of 10,498 `.py` lines (3,230 `src` production, 5,757 `tests/`, 1,511 conformance/mock/scaffold) [critic-2 N1] and 2,380 test files [01 §2][00 §Measured size][measured: git ls-files 'gateway/*.py' → 55 files / 10,498 lines; `gateway/src` alone is 3,230, and the 11-line `gateway/conformance/__init__.py` is the only file whose classification moves the split].

**Centre of mass.** The system's weight is the web tier, 53 % of production Python plus the 69,903 lines of production TypeScript, which ARCHITECTURE.md draws as one container box [01 §2][03 §2][critic G1]. The production frontend is about 14 % the size of production Python (69,903 / 489,459), not the 41 % that the all-files TypeScript total would suggest [measured: arithmetic on the two figures above]. As built, that tier is at least seven domains: composer, sessions, coordination, execution, app/auth, supporting domains and deployment [03 §2][X1 §6]. The audit core underneath (contracts, core, Landscape, engine) is the most disciplined part of the tree [X1 §0][X2b §Part A].

**Top conclusions.**

1. **The core layering is real and the module graph is acyclic.** `contracts` is a true leaf, core and engine import nothing upward, and 875 modules / 5,460 import-time edges contain 0 cycles, with a mutation control that proves the instrument sees cycles [X1 §0][X1 §1.2].
2. **What is not enforced is the risk.** Only `contracts < core < engine` is gated; everything else, about 71 % of production lines, has no import-direction gate [K024], and push CI never evaluates even that rule [K119]. With more developers incoming, this matters more than any single defect [ruling 6][05 §3].
3. **The audit machinery conforms where it was measured** (DB-clock authority, token fencing, the terminal-outcome model, audit primacy) [X2b §Part A][X2a §2][X2b §B1], but crash recovery aborts on a resumed gate [K051] and the "complete" export omits whole ledgers, though no evidence is lost [K040].
4. **Guided retirement is an extract-then-delete programme, not a delete-by-name.** Freeform fork and revert run on the guided-operations ledger [K012] and guided code fills ≥ 41 % of `sessions/service.py` [K016]; retiring the *mode* is not blocked by the tutorial, only optional full infrastructure removal is [K013].
5. **CI signal is degraded beyond the deliberate red.** The static-analysis job stops at a non-deliberate immutability red and skips 9 later gating steps [K116], three jobs never report [K118], and two rule sets never run [K117]; the trust-tier red itself is deliberate and not counted as a defect [ruling 8][05 §1].
6. **The baseline document under-describes the system.** ARCHITECTURE.md omits `web/` from its dependency graph [X1 §1.1], states stale epochs and table counts [K027], and presents unwired replay/verify as working [K056]; 264 slice delta rows, plus about 75 ADR conformance rows, were recorded against it and the ADRs [measured: sum of the "Delta rows" column of `temp/baseline-deltas.md` for S01–S25 → 264; independent recount of slice-section table lines minus header and separator rows → 264][critic G2].

**The six High findings** [VC §Outcome]:

| K-id | One line (verified claim) |
|---|---|
| [K051] | `GateExecutor` opens its `NodeStateGuard` at attempt 0, so a resumed fork-child that re-runs a gate hits the `node_states` uniqueness constraint and `Orchestrator.resume` aborts; reproduced end to end at the pin [K051]. |
| [K056] | `run_mode: replay/verify` and `replay_from` are validated and never consumed, so a YAML/CLI run configured for replay silently runs live with billable calls; `CallReplayer`/`CallVerifier` have no production consumer [K056]. |
| [K062] | The shared Jinja sandbox has no CPU or memory bound, and on the multi-user web surface one authenticated request can stall a whole replica at template compile time [K062]. |
| [K063] | 11 batch-aware plugins raise a bare `TypeError` on one wrong-type row value, which aborts the whole run (exit 4) instead of routing the row [K063]. |
| [K103] | `elspeth web` overwrites `ELSPETH_WEB__AUTH_PROVIDER` with its `--auth` default `local`: ACA/Entra crash-loops, and ECS upgrade mode (OIDC) silently boots on local auth with open registration behind an internet-facing ALB [K103]. |
| [K123] | The ECS Scenario C gateway sidecar gets 7 of the 13 configuration names it requires, so it cannot start, and the web container and runtime doctor that wait on it cannot start either [K123]. |

---

## 2. Scope, method and evidence chain

### 2.1 Scope and pin

- **Scope:** `src/elspeth/` (all Python packages plus `web/frontend`), `elspeth-lints/`, `gateway/`, `tests/` (structure only), `evals/`, `scripts/` gates and `deploy/` [00 §Analysis Configuration].
- **Not in scope (not analysed):** `design/` (a design-system bundle of 75 tracked files that the frontend does not import), `tools/pdf` (15 files of doc-pack build), `config/` beyond `config/cicd` (that is `config/mcp`, one file), `docs-archive/` (3 files), and the deploy and example scripts that no slice names (5 of 7 deploy scripts and 10 of 25 example scripts, by the critic's basename test) [measured: git -C <pin> ls-files <dir> | wc -l → design 75, tools/pdf 15, config/mcp 1, docs-archive 3][critic G15]. The critic's line counts for `design/` and `tools/pdf` used a narrower file filter and are not repeated here.
- **Pin:** every agent read the detached worktree at `85ebf2739`, because the live checkout moved from `780ef0f56` to `85ebf2739` during the scan [00 §Analysis Configuration][01 §Validation corrections]. The verification pass confirmed `git log 85ebf2739..release/0.8.1` was empty when the clusters were checked, so every finding also described the branch head at that time [VC §Outcome].
- **Partition:** 25 subsystem slices (S01–S25, the frontend split into three) plus four cross-cuts: X1 dependency and layering (measured), X2 ADR conformance and invariants (run as X2a and X2b), X3 test architecture and X4 existing-evidence consolidation [00 §Slice partition].

### 2.2 Method, phase by phase

| Phase | What ran | Output | Source |
|---|---|---|---|
| Discovery | Holistic scan, mechanical import matrix and SCCs; discovery validated (42 claims checked, 10 fixed) | `01`, `temp/import-matrix.md` | [00 §Execution Log][01 §Validation corrections] |
| Workflow A | 25 slice explorers + 5 group validators + 1 discovery validator (31 agents, 0 errors) | 25 catalog entries | [00 §Execution Log] |
| Group validation | Validators re-checked 369 catalog claims across the 25 entries and found 52 wrong (14 %); with the 01-discovery validation the totals are 411 / 62 (15 %). All were corrected in place; verdicts 4 APPROVED, 21 APPROVED_WITH_CORRECTIONS, 0 rejected | `temp/validation-G1…G5` | [00 §Execution Log][02 §header] |
| Workflow B | 5 cross-cut agents (X1, X2a, X2b, X3, X4); X1 corrected discovery §8.2 | `temp/cross-X*.md` | [00 §Execution Log][01 §8] |
| Concern extraction | Deterministic parser over the catalog concern tables: 385 rows = 16 High / 128 Medium / 241 Low / 0 Critical | `temp/verify-input.json` | [00 §Execution Log] |
| Adversarial verification (C1) | 179 units (16 High + 128 Medium rows + 35 cross-cut gap sections) deduplicated into 148 clusters; tier A (44 clusters) got a reproducer plus a counter-evidence hunter, tier B (104) one verifier; 203 agents | `temp/verified-concerns.*` | [VC §Method][00 §Execution Log] |
| Severity tiebreak | A neutral judge re-derived severity for the 7 split-severity clusters, the single-vote upgrade K062 and the re-typed K130 (9 agents) | final severities | [VC §Method][00 §Execution Log] |
| Synthesis (C2) | Diagrams (03), quality assessment (05), this report (04) and the architect handover (06), then final validation (9 agents) | `03`–`06` | [00 §Orchestration][00 §Execution Log] |
| Completeness critic | One independent pass over 01–06 and `temp/` for what is missing across the whole set: 18 gaps (1 High, 7 Medium, 10 Low), none changing a High, a refuted cluster or a verified severity | `temp/validation-completeness-critic.md` | [critic §3][00 §Execution Log] |
| R2 gap round (R2a) | 1 sweep agent plus 36 neutral single verifiers (37 agents) checked the defect-shaped items the extractor could not reach: behavioural baseline-delta rows, web-review ids split across slices as unverified Low rows, and orphan review Mediums. 31 were confirmed (8 Medium, 23 Low) and 3 refuted (K163, K180, K181), giving 34 new clusters K149–K182; 2 duplicates were merged into K069 and K010 | 34 new clusters in `temp/verified-concerns.*` | [VC §Method][00 §Execution Log] |

**Verification totals.** 249 verification agents ran (203 in C1, 9 tiebreak agents and 37 in the R2 round) [00 §Execution Log]. Individual votes were 168 CONFIRMED_WITH_ADJUSTMENT, 52 CONFIRMED and 6 REFUTED, with 1 verdict tiebreak and 9 severity tiebreaks [VC §Outcome]. The authority now holds 182 clusters: 148 from C1 (179 units) and 34 from R2 (K149–K182). Final severities are High 6, Medium 92, Low 79 and 5 refuted, 0 Critical [VC §Outcome][measured: python3 Counter over final_severity in temp/verified-concerns.json → Medium 92, Low 79, High 6, None 5; len → 182]. R2 added no High: its 8 Mediums are K150, K154, K155, K160, K161, K162, K166 and K182 [measured: final_severity of cid ≥ K149 → Medium 8, Low 23, None 3]. 9 of the 14 clusters reported as High ended below High, and one reported Medium (K062) ended at High [VC §Outcome][05 §1][measured: python3 Counter over (reported, final_severity) in temp/verified-concerns.json → ('Medium','High') 1, K062 only; VC's prose sentence says two, its transitions table says one]. By provenance, the 148 C1 clusters are 88 NEW, 29 TRACKED, 16 REPORTED-AND-TRACKED and 15 PREVIOUSLY-REPORTED; with R2 the totals by prefix are NEW 99, TRACKED 31, REPORTED-AND-TRACKED 16 and PREVIOUSLY-REPORTED 36 [VC §Outcome][measured: Counter over the leading word of `provenance` in temp/verified-concerns.json; R2 rows carry long provenance strings, so they are counted by prefix]. The whole analysis used about 248 agents and 30.9M subagent tokens before synthesis, at the user's explicit choice of fidelity over efficiency [00 §Agent count]; with C2 (9 agents, ~2.77M tokens) and the R2 round (37 agents, ~3.93M tokens) the total before this revision is 294 agents and about 37.6M subagent tokens [00 §Execution Log; arithmetic 31 + 5 + 203 + 9 + 9 + 37 = 294].

**Baseline comparison.** 264 baseline-delta rows from the 25 slice entries, plus about 75 rows in the X2a/X2b ADR conformance tables (~51 + ~24), were consolidated mechanically into `temp/baseline-deltas.md` [measured: sum of the file's "Delta rows" column for S01–S25 → 264; recount of slice-section table lines minus header and separator rows → 264][critic G2]. The execution log's earlier figure of 275 came from a header-row miscount and is corrected there [00 §Execution Log].

### 2.3 Limitations

- **Residual catalog error is assumed, not measured.** The 369-claim catalog validation sample found 14 % wrong (411 / 62 = 15 % with 01-discovery); the catalog header says this *suggests a comparable* residual rate among the claims not re-checked, so any catalog count, line citation or size not tagged to a K-id or a `[measured]` should be treated as ± [02 §header][00 §Execution Log].
- **Low catalog rows were not re-verified as a set.** The 241 Low rows in the catalog's concern tables did not go through C1 verification; the 79 Low *clusters* did (downgraded Mediums, cross-cut gaps and 23 R2 items) [VC §Method][05 §2.3]. The R2 round verified only the Low-row web-review ids that recurred across slices and the defect-shaped baseline-delta rows the critic found; the other catalog Low rows remain unverified [VC §Method][critic G4][critic G8].
- **Single-verifier Mediums.** The 104 C1 tier-B clusters had one verifier each, and so did every R2 cluster (one neutral verifier); only the 44 C1 tier-A clusters had two [VC §Method].
- **No planted-false-claim control.** Verification cannot show it *would* have refuted a false claim; confirmations rest on the evidence each verdict cites, not on the confirmation rate [VC §Outcome].
- **Frontend depth.** The frontend (69,903 production TS/TSX lines, plus 132,755 test lines) was covered by three slices, against about ten for the web backend; explorer confidence was Medium-High (S21), High (S22) and Medium overall (S23) [01 §6][05 §9][S21 §Confidence][S22 §Confidence][S23 §Confidence][critic G1].
- **Read depth elsewhere is also partial.** Each slice's §Confidence records what was read in full, sampled or only outlined; the thin spots that bear on this report are: **S24**, 19 of the 24 rule implementations read at metadata and description level only, `cli.py` about 1,000 of 6,110 lines, 6 workflows at trigger level only, and the `scripts/state_engine_*` family not read [S24 §Confidence]; **S20**, 19 of the 23 `_aws_ecs_acceptance` modules outlined only and Terraform/bicep not read beyond the listed files, although two of the six Highs (K103, K123) are deployment defects [S20 §Confidence][K103][K123]; **S06**, the seven declaration-contract modules outlined only and most of `sink.py` not read [S06 §Confidence]; **S03**, `lineage.py`, `reproducibility.py` and `export_mappers.py` not read and `query_repository.py` read for structure only [S03 §Confidence]; **S13**, the claim that the tutorial never uses deferred-intent management, component edit or correction actions is INFERRED, not traced [S13 §Confidence]. Consult the slice's §Confidence before relying on a catalog-only claim in these areas [critic G5].
- **Test trees were examined for structure and counts only.** `tests/` was in scope as structure only [00 §Analysis Configuration], and the same holds for the frontend and gateway *test* trees: 54 % of frontend test lines and 53 % of gateway test lines are never named in any deliverable [critic §2][critic G17].
- **Gate scripts not covered.** Four CI- or pre-commit-wired gate scripts and one workflow were never assessed; §3.6 names them [critic G7].
- **retired code index is stale and uninformative.** Its index is at `ee04378f8`, its last run failed, its "0 import cycles" comes from an empty import-edge set, and its 738 findings contain no architectural ones [X1 §0][X4 §D][01 §7].
- **No suite was run.** Test-architecture statements are structural, measured by collection or grep [X3 §Caveats & Required Follow-ups][05 §9].
- **Inferred consequences are inherited.** Where a cluster says a path was not executed (for example the lease-recovered path in K051, the RSS link in K049), this report inherits that limit [K051][K049].

---

## 3. Architecture as built

### 3.1 Layers and tiers

The intended hierarchy exists in two versions that disagree: ARCHITECTURE.md draws `contracts → core → engine / plugins / telemetry → UI` with engine and plugins as peers, while ADR-006 and the lint put plugins (L3) above engine (L2) [01 §5][X1 §1.1]. What the lint actually encodes is `contracts (L0) < core (L1) < engine (L2) < everything else (L3)`, checked by the `trust_tier.tier_model` rule L1, which also covers lazy imports, reports 0 findings at the pin and trips on mutation controls [X1 §0][X1 §5.1][03 §4a].

| Boundary | State at pin | Enforced by | Source |
|---|---|---|---|
| `contracts` is a leaf | 0 outbound edges of any kind | L1 rule; `test_leaf_boundary.py` | [X1 §1.2][X1 §5.1] |
| core → engine / plugins / web | 0 edges | L1 rule | [X1 §1.2][01 §5.1] |
| engine → plugins / web / UI | 0 edges | L1 rule | [X1 §1.2] |
| plugins → engine | 3 edges (`runtime_factory` → `engine.orchestrator.preflight` module + lazy; `_diversion_attribution` → private `engine._error_hash`) | legal under ADR-006, unadjudicated against ARCHITECTURE.md | [X1 §1.2][X1 §5.2][05 §3] |
| UI → web (CLI 22 lazy, `cli_plugins` 2, `composer_mcp` 18 module-level) | the CLI and composer MCP use web as a library | nothing | [01 §5.1][X1 §1.2][K024] |
| Module-level acyclicity | 0 SCCs over 875 modules | nothing | [X1 §0][X1 §5.2] |
| Web-internal and frontend layering | 98.3 % role-layered (web); 21-bucket directory SCC (frontend) | nothing | [X1 §2.7][K110] |

**The web "cycle" is a bucketing effect.** All 15 `web.*` packages form one package-level SCC, but no module-level cycle causes it: the web packages are vertical slices spanning 25–33 DAG levels each, so cross-slice imports at different heights produce package cycles while every module import points down [X1 §0][01 §8][03 §4b]. Classified by role (domain < service < routes < app), 1,898 of 1,931 web-internal edges point down; verification corrected the 33 upward edges to 11 from two misfiled Azure acceptance modules, 2 from the `shareable_reviews` package `__init__` and 20 domain → service [X1 §2.7][K144]. Runtime cycles exist only through lazy imports, and only 41 of 701 lazy imports are mechanical cycle-breakers [X1 §0][K145].

**Two web modules act as contracts without being contracts.** `web.composer.state` (9,064 lines, fan-in 71) and `web.sessions.protocol` (5,051 lines, fan-in 56) carry contract-level fan-in inside feature packages [X1 §2.2]. Web-domain vocabulary has also drifted into L0 `contracts` with no ownership record [K036].

### 3.2 Two authoring surfaces over one runtime

- **YAML + CLI.** The `elspeth` Typer CLI has 17 leaf commands, including the multi-worker and recovery verbs `join`, `abandon`, `export-resume`, `purge` and `health` [S09 §Baseline delta]. `config_loading.py`, the shared CLI and web settings loader and a Tier-3 boundary, is not mentioned in ARCHITECTURE.md [S09 §Baseline delta].
- **Web Composer.** One `ComposerServiceImpl` routes each request to one of two provider-driven surfaces: a bounded one-shot planner when the state is structurally empty and the message is an explicit mutation, otherwise a five-phase compose loop over a 42-tool surface [S10 §Surface selection][S11 §Baseline delta][03 §3a]. The server validates, redacts, audits and gates; its only server-authored structure is the sanctioned required-control splice [X2b §B3][K023][03 §5a].
- **Where they converge.** Web execution turns a persisted `CompositionState` into an admitted, audited engine run, building one `Orchestrator` per run, the same engine the CLI drives [S16 §Responsibility][S05 §Public interface / entry points][03 §2][03 §3c-i]. ADR-040's three validation surfaces hold: Stage 1 `CompositionState.validate`, Stage 2 `validate_pipeline` (23 ordered checks, then advisories and a bounded source proof) and the executor [S10 §Baseline delta][S16 §Baseline delta].
- **Where they diverge.** The web importer drops `run_mode` and `replay_from`, so only the YAML/CLI surface reaches the unwired replay/verify path [K056]; YAML import is lossy by design (edges, `metadata`, `landscape` and three coalesce fields) [S12 §Baseline delta]. Composer Stage 1 keeps hand-maintained twins of the core/dag guarantee, extras and type walks, with parity held by a 339-site manifest and comments rather than a shared primitive [K002].
- **People and external systems.** Four human roles have their own entry points (pipeline operator, composer user, auditor/approver, administrator), and ELSPETH calls LLM providers, the gateway, object storage, document and safety services, identity providers and observability back ends; diagram 03 §1 draws this context [03 §1][S09 §Public interface / entry points][S18 §Authentication model: three stores, one token].
- **Other surfaces.** A read-only Landscape MCP server (33 tools) and a composer-as-MCP server (1,850 lines) that loads 118 `elspeth.web` modules and hand-mirrors the web audit envelope [S09 §Baseline delta][K071]; a Textual TUI reached only from `explain`, which cannot open a PostgreSQL Landscape [K072].

### 3.3 The web tier's real decomposition

Diagram 03 §2 draws the web tier as seven domains [03 §2]. Measured slice sizes:

| Domain | Slice(s) | Size at pin | What it owns | Source |
|---|---|---|---|---|
| Composer core loop | S10 | 39,223 lines / 24 files | LLM loop, planner, `CompositionState`, provider transport | [S10 §header][S10 §Responsibility] |
| Composer tools | S11 | 22,586 / 18 | 42-tool registry, dispatch, tool planes | [S11 §header][S11 §Baseline delta] |
| Composer governance | S12 | 22,786 / 58, plus 1,716 lines of skills | advisor END gate, required controls, custody/redaction, proposal → commit, YAML, composer audit, tutorial run | [S12 §header] |
| Guided lane (retiring) | S13 | `composer/guided` 21,460 / 20; ~35K lines of guided backend in all | guided wizard, the tutorial's backend | [S13 §header][K013][X1 §7] |
| Sessions domain | S14 | 32,480 / 27 | `SessionServiceImpl` (10,546-line class), session schema | [S14 §header][K083] |
| Sessions HTTP routes | S15 | 23,867 / 24 (≈12,560 excluding guided) | 61 routes, 7 routers, request-lifecycle machinery | [S15 §header][S15 §Baseline delta] |
| Execution | S16 | 20,219 / 34 | Stage 2 preflight, launch admission, the single run worker, progress streaming, run recovery | [S16 §header] |
| Coordination | S17 | 16,504 / 27 | ~20 Sessions-DB write authorities, fences, leases, membership, run-start saga | [S17 §header][S17 §Baseline delta] |
| App composition, auth, identity | S18 | 20,922 / 46 | `create_app`, four IdP profiles + local, session tokens, secrets | [S18 §header][S18 §Baseline delta] |
| Supporting domains | S19 | 15,823 / 36 | blobs, plugin policy, catalog, shareable reviews, audit readiness | [S19 §header] |
| Deployment and ops | S20 | 3,512 runtime + 19,900 acceptance | deployment contract, doctor, readiness, schema probe, acceptance harnesses | [S20 §header] |
| Frontend | S21–S23 | 69,903 production TS/TSX lines in 259 files (+132,755 test lines in 338 files) | Zustand stores, API client, chat/tutorial, workspace shell, E2E harness | [01 §2][S21 §Baseline delta][S23 §Baseline delta] |

The heaviest cross-domain couplings are verified concerns: `web.coordination ↔ web.sessions` 40 ⇄ 40 with private symbols both ways and session-table DML split across the two [K034]; `web.execution ↔ web.composer` 23 ⇄ 16 with execution taking private `composer.state` helpers [K142]; `web.sessions ↔ web.composer` 19 ⇄ 133, 60 % of it guided [K141]; `web.auth ↔ web.(root)` 20 ⇄ 21 [K143][03 §4b]. Execution is a peer writer of the Landscape as well as a caller of the engine: it opens the Landscape at 10 sites and takes run-coordination seats [S16 §Baseline delta][03 §2]. Diagrams 03 §3a (composer) and 03 §3b (sessions and coordination authority stack) draw the component level [03 §3a][03 §3b].

### 3.4 Data stores

| Store | Technology | Shape at pin | Notes | Source |
|---|---|---|---|---|
| Landscape audit DB | SQLite / SQLCipher / PostgreSQL | 47 tables, schema epoch 43 | ARCHITECTURE.md says 46 / epoch 38; no runtime code reads the PostgreSQL server version, while the ACA bundle defaults to PostgreSQL 17 and every PostgreSQL proof runs on 16 | [S03 §Baseline delta][K027][K154] |
| Sessions DB | SQLite WAL / PostgreSQL | 47 tables in 10 domains, epoch 66 | also the cross-replica coordination store and the composer's audit trail; the baseline gives it no epoch or table count; it also holds the ADR-034 inline-blob resolution provenance, which never reaches the Landscape | [S14 §Baseline delta][S17 §Baseline delta][S12 §Baseline delta][K155] |
| `auth.db` | raw `sqlite3` under `data_dir` | local credentials only; no epoch | opened in every state mode when `auth_provider == local`, including external PostgreSQL | [S18 §Data & persistence][K098] |
| Core payload store | content-addressed filesystem | share snapshots, library | retention lives in `core/retention/purge.py`, not in the store | [S02 §Baseline delta][S19 §Baseline delta] |
| Web blob custody store | filesystem `data_dir/blobs/<session>/…` | quotas, deletion and replacement ledgers, fences | not in the baseline; `blobs_table` has two writer packages | [S19 §Baseline delta][K100] |
| Landscape JSONL journal | opt-in files, per worker | outbox drained after commit | the postcommit drain runs inside a patched `do_commit` | [S03 §Baseline delta][K043] |
| Remote-effect spool | filesystem | CWD-relative default | no deploy sets `ELSPETH_EFFECT_SPOOL_DIR` | [K059] |

The Sessions DB and the Landscape share no transaction; the run-start saga spans them [S17 §4. Cross-database run-start saga][03 §2]. Diagrams 03 §3d-i and 03 §3d-ii draw the Landscape composition root and ledgers [03 §3d-i][03 §3d-ii].

### 3.5 Runtime and concurrency model

**Durable scheduler.** Token work lives in `token_work_items`, one row per run, token, node and attempt, moving through READY, LEASED, PENDING_SINK and BLOCKED; every lifecycle transition appends a `scheduler_events` row on the same connection [S04 §Durable token scheduler]. Barrier adoption and adoption-marker reset are the exceptions that write no event [K044]. Every ledger mutation takes a leader `CoordinationToken` or a member `WorkerMembershipToken` and fences first [S04 §Responsibility][03 §3d-ii]; ADR-048 fencing conforms at 89 of 91 mutation APIs, the other two being named establishment exceptions [X2b §Part A]. Every deadline issuance in `core/landscape` derives from a database clock sample (ADR-047; the gate passed 499 of 499) [X2b §Part A][05 §8].

**Leader and followers.** Each run has exactly one seat-holding leader; followers join only through `elspeth join`, claim-only, on one host with SQLite; the web tier hosts only leaders [S05 §Multi-worker shape][03 §3c-i][03 §7c]. Inside a worker, one drain thread runs the source loop, row processing and sink writes, beside a heartbeat thread (15 s beat, 80 s window) and an idle-timeout pump; other processes interact only through the DB (CAS, epoch and membership fences, leases) [S05 §Concurrency model][S05 §Multi-worker shape]. Followers are not equivalent to the leader: they skip the expand-width fence [K047] and never retry [K048], and a PostgreSQL follower join is not refused [K137]. Diagram 03 §5c draws the crash-resume path, on which K051 and K052 sit [03 §5c][K051][K052].

**Row processing and barriers.** `RowProcessor` builds the executors and coordinators; all four barrier kinds (aggregation, coalesce, row union, collector) are journal-first: arrive without a durable record, get adopted by the leader's next intake pass, fire in one atomic `complete_barrier` transaction and restore from the journal on resume [S06 §Barrier family][03 §3c-ii]. The collector is the one barrier without a residual receipt, so a crash between its flush and `complete_barrier` is unrecoverable [K052]. The token terminal model is ADR-019's two axes, 3 outcomes × 16 paths with 14 legal pairs, checked on write and read in Python only [03 §6a][X2a §2][K026].

**Sink effects.** Sinks publish only through the recoverable effect protocol: reserve, prepare, lease with a heartbeat thread at TTL/3, commit or reconcile, atomic finalize [S06 §Sink effect coordinator][03 §3c-ii][03 §6b][03 §6c]. All 9 built-in sinks raise from `write()`, and the real contract is the typed `SinkEffectProtocol`; the `BaseSink` docstring and abstract methods still teach `write`/`flush` [S07 §Baseline delta][K057]. Sink writes happen after the source loop, so every sink-bound token is held in memory in `pending_tokens` until the loop ends [K049].

**Web process model.** One uvicorn worker per container, refused at boot otherwise; one pipeline per process (`ThreadPoolExecutor(max_workers=1)`), so other admitted runs wait `pending` [S18 §Concurrency model][S16 §Concurrency model]. That queue is an unbounded FIFO per replica with no per-user or global depth limit, no fairness and no run-duration cap, and neither the baseline nor the operator docs say runs are serialised; one long run delays every other user's run on that replica [K150]. Authenticated requests cross a shared 16-thread worker pool with 32 admission slots [S18 §Concurrency model]; saturation surfaces as 500 rather than 503 on the auth path [K097]. Replicas scale out only through PostgreSQL: membership leases, a shared rate limiter, DB-backed composer progress and WebSocket tickets [S18 §Concurrency model][03 §7c]. Session locking is three layers: a process-local lock, a DB transaction lock (`pg_advisory_xact_lock` on PostgreSQL) and row `FOR UPDATE` [S17 §6. Concurrency model]. On PostgreSQL the server polls `run_events` every 0.25 s to feed the run WebSocket [S16 §Two streaming modes][03 §5b]. Synchronous DB or file work on the event loop stalls every request in three verified places [K086][K090][K096].

**Deployment topology.** Compose and `linux-systemd` single-host bundles; AWS ECS/Fargate with one web task and no overlap; Azure Container Apps with a single sticky revision and 2–4 production replicas [S20 §Deployment view][03 §7a][03 §7b], whose supported status three sources state differently [S17 §Baseline delta]. The ACA acceptance probe can only qualify a 2/2 topology, never production's 2/4 [K105]; peer compatibility columns are written and no production code reads them for a peer check, but session and Landscape epoch mismatches are still caught at start by the schema-identity check, and only a `coordination_protocol` bump with no schema change is unguarded, which is latent while the protocol is at v1 [K093]. Nothing refuses a second concurrently active web replica on targets documented as single-replica [K152]. Diagram 03 §7c tabulates the supported limits and how each is held [03 §7c].

### 3.6 The trust-tier model in practice

- **Tier 1 (audit data, "our data").** 15 Tier-1 error classes are registered, three of them in `core/landscape/errors.py` [S01 §Baseline delta]. Sampled Landscape handlers re-raise rather than absorb [X2b §B2]; X2b sampled three Tier-1 reads that apply defaults or coercion [X2b §B2], and verification kept only one as a real Tier-1 leniency: the MCP `list_collisions` read of `union_field_origins` from `context_after_json`, which reports `winner_branch=None` instead of raising on a corrupt row; the export timezone replace is a SQLite dialect normalisation and the interpretation-kind default applies to Tier-3 composer data [K140]. The Landscape exporter's promise of a complete export is not met, though the DB keeps every row [K040].
- **Row content leaving the audit boundary.** When an operator sets `tracing.provider: langfuse` on an LLM transform or LLM source in CLI/YAML, every call's prompt (templated row data included) and response go to the configured Langfuse host, with no content-recording switch; this is documented and operator-opt-in, and web-authored pipelines cannot enable tracing. The `azure_ai` half does not reach App Insights on the current call path. Verified as a Low privacy posture and a baseline gap, not a defect [K170][critic G3].
- **Tier 3 (external data).** Composer tool arguments are rejected, not coerced, contrary to the baseline diagram's "coerce where possible" [S11 §Baseline delta]. The auth side has 8 declared Tier-3 boundary sites the baseline diagram omits [S18 §Baseline delta]. Weak Tier-3 spots are verified: three divergent CSV/JSON parsers [K058], unchecked frontend casts at the browser boundary [K033], one ADR-032 `runtime_checkable` gate on the botocore S3 body [K136], a credential regex that rejects ordinary dotted identifiers [K101], and a secret scrubber that misses AWS secret keys and bare `sk-ant-` keys [K035].
- **Input bounding on multi-user surfaces.** Jinja compile and render are unbounded [K062]; the expression parser raises a raw `RecursionError` on deep input with no length or depth cap [K037]; the string-amplification guard runs only on the composer preview path [K038].
- **Enforcement.** The lint engine registers R1–R9, the `trust_boundary.*` honesty rules and the masquerade gate, and ADR-010's nominal `AuditEvidenceBase` [S24 §Baseline delta]. The live registry was measured (24 rules), but 19 of the 24 rule implementations were read at metadata and description level only, so "registered" is the measured claim and what each rule enforces was not read [S24 §Confidence][critic G5]. Four gate scripts and one workflow that CI or pre-commit runs were never assessed by any slice: `scripts/state_engine_plugin_matrix.py` (616 lines, run at `ci.yaml:1027` against the golden plugin-lifecycle matrix), `scripts/cicd/check_redaction_direction.py` and `scripts/cicd/assert_redaction_label.py` (run by `composer-redaction-gate.yml:77` and `:95`), `scripts/git-hooks/pre-commit-secret-scan.sh` (the pre-commit secret scanner, `.pre-commit-config.yaml:63`), and the workflow `enforce-telemetry-backfill-trailer.yaml`, whose trigger was the only part S24 read [measured: ls and grep of those paths and wire sites at the pin][S24 §Confidence][critic G7]. Whether they pass at the pin is unknown. The trust-tier CI red is a deliberate fail-closed state and is not counted as a defect here [ruling 8][05 §1]. The finding is narrower: at the pin the job halts at a non-deliberate immutability red before reaching the trust-tier step [K116]; on push the tier-model step exits 2 at allowlist load, so the layer rule is never evaluated [K119]; signing can no longer turn push CI green since the key left the workflows [K120]; and the allowlist carries 201 stale and 10 expired entries with per-file rules at the 37/37 ceiling [K122].
- **Audit primacy** holds at all 5 sampled telemetry emissions: telemetry is emitted after the Landscape write [X2b §B1].

---

## 4. Intended vs built

`temp/baseline-deltas.md` holds 264 slice delta rows plus about 75 rows in the ADR conformance tables [measured: sum of its "Delta rows" column for S01–S25 → 264][critic G2]. They fall into three themes. The rows were extracted, not verified; the R2 round verified the defect-shaped ones the critic found (for example K154, K155, K171) and the rest remain unverified [VC §Method][critic G4].

### 4.1 What ARCHITECTURE.md and the ADRs get right

- **The leaf and the core layering.** "Contracts has ZERO outbound dependencies" holds at every import kind, and core has 0 upward rows [S01 §Baseline delta][S02 §Baseline delta].
- **Audit-first ordering.** "Events emitted AFTER Landscape recording" holds for run-level events, and sinks are written after the row loop as the sequence diagram shows [S05 §Baseline delta][X2b §B1].
- **Several ADRs hold exactly.** ADR-038 abandon semantics, ADR-040's three validation surfaces, ADR-020's retirement of batch-LLM transforms, and ADR-047/048 in code [S05 §Baseline delta][S10 §Baseline delta][S08 §Baseline delta][X2b §Part A]. Across all 47 present ADRs, 29 are conformant, 11 partial, 3 drifted, 1 superseded, 1 retired and 2 unverifiable [05 §6][X2a §1][X2b §Part A].
- **Sizes of the stable containers** are close: core 23,807 vs ~23,400, checkpoint 2,406 vs ~2,400, TUI 2,334 vs ~2,300, telemetry 3,872 vs ~3,800 [S02 §Baseline delta][S09 §Baseline delta].
- **The gateway boundary.** `gateway/` is standalone in both import directions, as stated [S25 §Baseline delta].
- **The deployment contract.** Validate-only startup, the doctor, readiness checks and schema-owner separation hold, and are generalised beyond AWS [S20 §Baseline delta].

### 4.2 What it omits

- **The web tier's internals.** The baseline's L2 diagram has 12 container boxes, one of which is "Web app + Composer" [measured: sed -n 107,165p ARCHITECTURE.md | grep -c '^        Container(' → 12]; discovery counted 11 [01 §6], and the baseline's own responsibilities table lists 13 code containers including Testing, so the baseline's diagram and table disagree [measured: sed -n 166,186p ARCHITECTURE.md]. That box holds 53 % of production Python [01 §2]: coordination (16,504 lines, about 20 authority classes) [S17 §Baseline delta], execution (20,219 lines) [S16 §Baseline delta], the 23,867-line session HTTP edge [S15 §Baseline delta], the 42-tool registry and redaction manifest [S11 §Baseline delta], and the supporting domains [S19 §Baseline delta].
- **The frontend.** 69,903 production TS/TSX lines (plus 132,755 test lines) with no component, state, routing or test-harness description anywhere in the baseline [S23 §Baseline delta][S21 §Baseline delta][critic G1].
- **The multi-replica substrate.** Membership, fences, read admissions, run-start permits, cross-replica tickets and progress exist in code; no ADR records the fence, permit or membership design [S17 §Baseline delta][S14 §Baseline delta].
- **Engine and Landscape components.** About 11 orchestration coordinators, about 7,000 lines of barrier-family components, the sink-effect adapter half inside plugins, the runtime-VAL manifest, and Landscape sub-packages holding 49 % of the package [S05 §Baseline delta][S06 §Baseline delta][S07 §Baseline delta][S01 §Baseline delta][S03 §Baseline delta].
- **Whole subsystems.** The enforcement subsystem (45.6K lines of analyzer, the signing seam, 3 MB of signed allowlists, the CI job graph) [S24 §Baseline delta]; `composer_mcp` [S09 §Baseline delta]; `evals/`, `examples/` and `website/`, the last a runtime dependency of the tutorial [S25 §Baseline delta]; about 20K lines of acceptance harness shipped in the wheel [S20 §Baseline delta].
- **Composer governance.** The advisor END gate, structured checkpoint output and the redaction manifest have no ADR [S12 §Baseline delta][K082]; the composer's audit trail lives in the Sessions DB, which the baseline does not say [S12 §Baseline delta].

### 4.3 What it states wrongly

- **Stores and epochs.** 46 tables and epoch 38 are now 47 and 43; the Sessions DB (epoch 66) and a third store, `auth.db`, are missing; `landscape.md` contradicts itself [K027][S03 §Baseline delta][S14 §Baseline delta][S18 §Baseline delta].
- **Token model.** The single-axis terminal-state table (COMPLETED, ROUTED, FORKED …) is retired in favour of the ADR-019 two-axis model [S04 §Baseline delta][S06 §Baseline delta][03 §6a].
- **Features presented as working.** Replay and verify run modes [K056]; four audited clients including a replayer and verifier with no production consumer [S07 §Baseline delta]; a "checkpoint API" on plugin contexts that no longer exists [S01 §Baseline delta].
- **Flows.** Telemetry is drawn through an `EventBus`, but the engine calls the telemetry manager directly and BLOCK backpressure waits 30 s then drops [S09 §Baseline delta]; the Jinja field-extraction example returns `frozenset()`, not the listed fields [S02 §Baseline delta]; the payload store has no retention logic of its own [S02 §Baseline delta].
- **Deployment facts.** "ACA deferred" against an implemented 2–4 replica profile; a Cognito container where the code has a generic OIDC profile used only in Scenario B; "one web task at a time" as a stated limit the code does not enforce [S20 §Baseline delta][S18 §Baseline delta][S17 §Baseline delta].
- **ADR status and prose.** ADR-019's run-status predicate drifted without amendment [K130]; ADR-026/029/035/038 carry superseded prose, and ADR-037 gained a sixth `InterpretationKind` member without its required assessment [K029]. ADR-048's "Proposed" status is not drift, because Task 8B has not landed; what remains is release-notes silence, since the 0.8.0 CHANGELOG promised ADR-048 would finish in 0.8.1 and the 0.8.1 section does not mention it [K028].

### 4.4 The most consequential deltas

| Baseline or docs say | As built | Why it matters | Source |
|---|---|---|---|
| Web app + Composer is one container | ≥ 7 domains, 53 % of production Python, plus 69,903 production TS/TSX lines (+132,755 test) | the system's centre of mass has no architecture description | [01 §2][03 §2][S23 §Baseline delta] |
| ADR-006: strict layering, CI-enforced | only L0–L2 gated; ~71 % of production lines ungated; push CI never evaluates L1 | nothing turns red when a boundary erodes | [K024][K119][S24 §Baseline delta] |
| Replay / verify run modes | accepted and silently run live | billable calls and live sink writes under a "replay" config | [K056] |
| Exporter gives a complete audit export | omits run sources, barrier receipts, coordination ledgers, preflight and admissions | a portable export falls short of its compliance promise | [K040][S03 §Baseline delta] |
| Env vars configure every web setting | `elspeth web --auth` default overwrites the auth provider | SSO deployments crash-loop or silently run local auth | [K103][S20 §Baseline delta] |
| One web task; ACA deferred; replicas unsupported | multi-replica substrate implemented; ACA at 2–4 replicas; three sources disagree | the supported topology is undecidable from the docs | [S17 §Baseline delta][S20 §Baseline delta][K105] |
| Composer invariant 1 carve-out: "required-control admission gates" | required controls insert server-authored nodes at five seams | the one structural insertion is sanctioned only by a ticket and code comments | [K023][S12 §Baseline delta] |
| ADR-031: tutorial canaries "the same guided machinery every user exercises" | after retirement no ordinary user exercises guided; ADR-031 is unamended | the canary's guided planner surface is now driven only by the tutorial, although it still exercises the shared planner, commit and validation code; freeform, now the only user surface, has no fixed-script canary; the tutorial also fetches its fixtures from public GitHub Pages at runtime | [S13 §Baseline delta][ruling 2][K160][K161] |
| ADR-034: inline-blob resolutions kept in "a dedicated audit table" | that table is in the Sessions DB, is write-only, and never reaches the Landscape or its export | the Landscape shows what text reached a plugin but not that it came from a pinned inline blob; the gap is against audit primacy, not the ADR's wording | [K155] |
| Audit DB: 46 tables, epoch 38 | 47 tables, epoch 43; Sessions DB epoch 66; `auth.db` | operators and reviewers work from wrong schema facts | [K027][S14 §Baseline delta] |
| Sink lifecycle `write → flush` | effect protocol only; every sink's `write()` raises | a sink written from the docstring fails at preflight | [K057][S07 §Baseline delta] |
| Terminal states, single axis | two-axis outcome × path | lineage readers use retired vocabulary | [S04 §Baseline delta][03 §6a] |
| Enforcement not described | 45.6K-line analyzer, signing seam, CI graph | new developers meet the gates with no map | [S24 §Baseline delta] |

---

## 5. Architectural qualities

The quality assessment rates eight dimensions; the ratings are its own synthesis, not concern severities [05 §1].

| Dimension | Rating | Basis | Source |
|---|---|---|---|
| Audit integrity and correctness | Adequate | terminal model, DB-clock authority and token fencing conform; resume abort, export omissions, batch-plugin run aborts | [05 §1][K051][K040][K063] |
| Security and trust boundaries | Weak | two Highs on authenticated or shipped surfaces; scrubber misses; unbounded expression evaluation | [05 §1][K062][K103][K035][K037][K038] |
| Architectural structure and coupling | Adequate | acyclic module graph and true leaf; web package SCC; only L0–L2 enforced | [05 §1][X1 §0][K024] |
| Maintainability and complexity | Weak | flagged files kept growing; 2,000+-line functions; 5,000–10,500-line god objects | [05 §1][K075][K019][K083][K055] |
| Testability and test architecture | Adequate | bottom-heavy suite, 38 whole-tree gates, mock-discipline baseline; E2E never drives the LLM loop; selections never run | [05 §1][X3 §1.1][X3 §3.2][K030][K031] |
| Operability and deployment | Weak | two Highs make shipped deployments fail or boot insecurely; ACA probe and PostgreSQL TLS gaps | [05 §1][K103][K123][K105][K106] |
| Enforcement and governance | Weak | CI halts at a non-deliberate red and skips 9 gating steps; 3 jobs skipped; 2 rule sets never run; L1 not evaluated on push | [05 §1][K116][K117][K118][K119] |
| Documentation fidelity | Weak | `web/` absent from the dependency graph; replay/verify presented as working; ADRs fare better | [05 §1][X1 §1.1][K056] |

### 5.1 Strengths

- **Layering where it is enforced is clean.** `contracts` has 0 outbound edges, core and engine have 0 upward edges, telemetry imports only contracts, and the L1 rule's mutation controls fire [X1 §1.2][X1 §5.1][05 §8].
- **Acyclicity without a gate.** 0 module-level SCCs, and web is already 98.3 % role-layered with no gate driving it [X1 §0][X1 §2.7].
- **Audit mechanisms conform in code.** ADR-047 DB-clock authority (499/499), ADR-048 token fencing (89 of 91 APIs, 721/721), a sound 7-contract declaration registry asserted at bootstrap, and a terminal-outcome model checked on write and read [X2b §Part A][X2a §2].
- **Composer invariant 1 holds.** The only `provider="server"` site is a refusal, and a name search for synthesis or fast-path functions returns 0 with a positive control [X2b §B3].
- **Architecture is a tested property.** 38 test files walk the tree through a sanctioned walker, authority boundaries are pinned manifests, and unspecced mocks fail the whole repository [X3 §3.2][X3 §8.2].
- **Verification discriminated.** 5 clusters refuted (2 in C1, 3 in R2), 9 of 14 reported Highs downgraded, 168 of 226 votes adjusted the claim text [VC §Outcome][05 §8].

### 5.2 Weaknesses

- **Unenforced structure.** Every boundary above engine holds today, and nothing would turn red if one stopped holding [K024][05 §3]. The frontend has no import-boundary rule and a 21-bucket directory SCC [K110].
- **God objects and growth.** `web/sessions/service.py` grew from ~14,321 to 15,006 lines and `web/composer/service.py` from ~10,298 to 11,377 against the baseline's own improvement item [01 §8][05 §4.1]; `SessionServiceImpl` is 10,546 lines with three transaction-ownership models [K083]; `RowProcessor` is 5,161 lines with extracted engines reaching 33 private members [K055]; `ChatPanel` is a 3,094-line function [K108].
- **Duplicated authorities.** Stage-1 validation mirrors the core/dag walks by hand [K002]; session-table DML is split across two packages [K034]; `blobs_table` has two writers [K100]; external-call recording uses several conventions, none of them structural [K061].
- **Multi-worker non-equivalence.** Row outcomes can depend on which worker claims a row [K047][K048].
- **Test gaps where the product is most complex.** Browser E2E never drives the composer LLM loop and 5 specs have never run [K030]; NFR benchmarks that ADR-010 calls its enforcement mechanism never run in CI [K031]; `web/` has no coverage floor [X3 §8.1].
- **Governance for more developers.** Release branches unprotected, no CODEOWNERS, 0 required approvals and no PR process documented in CONTRIBUTING at the time of X4; the `main` ruleset does require a PR and binds admins [X4 §F][05 §7].

---

## 6. Subsystem highlights

Sizes are from each entry's header; the finding is the slice's most important verified cluster [VC §Outcome]. Full detail in `02-subsystem-catalog.md` and `05 §2`.

### S01 — Contracts (L0)

34,196 lines in 96 files: the shared vocabulary of audit DTOs, the terminal model, protocols, the error taxonomy and the declaration-contract framework [S01 §header][S01 §Responsibility]. The leaf claim holds at every import kind [S01 §Baseline delta]. The terminal invariants (14 legal pairs, `completed` XOR outcome) are enforced only in Python on write and read, with no DB CHECK [K026]; the audit scrubber misses AWS secret keys and bare Anthropic keys [K035]. Web-domain and guided-lane types are drifting into L0 with no ownership record [K036].

### S02 — Core (non-Landscape)

26,755 lines in 55 files: settings, DAG construction, schema shape, canonical JSON, expression parser, payload store, checkpoint, retention and rate limiting [S02 §header]. The expression parser's typed-error contract is not total: deep input raises a raw `RecursionError` and nothing caps length or depth [K037], and the string-amplification guard is not applied at runtime [K038]. `build_execution_graph` never calls `graph.validate()`, so every caller must remember to [K039]. The baseline's Jinja extraction example is wrong [S02 §Baseline delta]. A 19-module checkpoint ↔ landscape cycle appears once parent `__init__` edges and the eager facades are modelled; verification found no import-order hazard at the pin and rates it structural hygiene with a measured cold-import cost, not a defect [K025].

### S03 — Landscape database, schema and repository facades

19,798 lines in the 34 top-level modules; the Landscape is 47 tables at epoch 43, not the baseline's 46/38 [S03 §header][S03 §Baseline delta]. The exporter's "complete" export omits run sources, barrier receipts, coordination and worker ledgers, preflight and run-start admissions, though the DB keeps them all [K040]. The opt-in journal's postcommit drain can make a durable commit look failed [K043], and local-account admin actions leave audit gaps [K011]. No runtime code checks the PostgreSQL server version: ADR-041 qualifies only PostgreSQL 16 (for AWS), the ACA bundle defaults to 17, and every PostgreSQL proof in the repo runs on 16; no PG 17 failure has been shown [K154].

### S04 — Landscape ledgers

19,155 lines in 33 files: the durable token scheduler and barrier journal, the sink-effect publication ledger and row/token/lineage aggregates, every write fenced to a coordination token and stamped against the DB clock wherever a lease decision is involved [S04 §header][S04 §Responsibility]. ADR-048 fencing conforms at 89 of 91 mutation APIs [X2b §Part A]. Barrier adoption and marker reset write no `scheduler_events` row [K044]; the terminal and status invariants have no DB-level CHECKs [K026]. K045 was refuted [VC §Outcome].

### S05 — Engine orchestration

18,994 lines in 54 files; the `Orchestrator` facade wires about 11 coordinators, none of which the baseline shows [S05 §header][S05 §Baseline delta]. Followers run without the expand-width fence [K047] and never retry, so a row's outcome depends on which worker claims it [K048]. Every sink-bound token is held in memory until the source loop ends [K049]; follower wiring is hand-assembled in `cli.py` [K050].

### S06 — Engine row processing and barriers

24,168 lines in 31 files, with four journal-first barrier kinds unified at the coordinator level [S06 §header][S06 §Baseline delta]. **High:** `GateExecutor` ignores resume attempt offsets, so crash recovery aborts on a UNIQUE collision; the same class of defect was already fixed for `AggregationExecutor` [K051][05 §2.1]. A crash between collector flush and `complete_barrier` is unrecoverable [K052], and `notify_empty_group` has no production caller [K053].

### S07 — Plugin infrastructure, sources, sinks, LLM support

33,871 lines in this slice of the 66,811-line `plugins/` package [S07 §Baseline delta]. **Two Highs:** replay/verify are validated and never consumed [K056], and the Jinja sandbox has no CPU or memory bound while web users author templates [K062]. Three divergent Tier-3 CSV/JSON parsers [K058] and external-call recording by convention rather than structure [K061] are the Medium themes. Every sink's `write()` raises; publication is through the effect protocol only [S07 §Baseline delta][K057].

### S08 — Plugins: transforms

32,940 lines in 80 files; discovery returns 38 transforms, 9 sources and 9 sinks [S08 §header]. **High:** 11 batch-aware plugins abort the run on one wrong-type value; only `batch_stats` converts the error [K063]. `TransformResult.error(retryable=True)` is inert [K065], and the profile-only `azure_ai_search` probe failure is real in raw Stage 1 but hidden from the web author by dispatch-boundary re-validation [K001].

### S09 — Operator surfaces

19,572 lines in 47 files: CLI, config loading, TUI, Landscape MCP, composer MCP, telemetry and the shipped testing kit [S09 §header]. The CLI is a 4,802-line monolith with copy-shaped command bodies [K073]; `TelemetryManager.flush()` can hang forever after its export thread dies [K070]; `explain` and the TUI cannot open a PostgreSQL Landscape [K072]. The composer MCP server hand-mirrors the web audit envelope and has already drifted [K071].

### S10 — Composer core loop

39,223 lines in 24 files: planner or compose loop, `CompositionState`, provider transport [S10 §header][S10 §Surface selection]. `composer/service.py` fuses six responsibilities, including about 2.9K lines of advisor code [K074]; elsewhere in the slice, `state.py`, `tool_batch.py` and `pipeline_planner.py` hold functions of 1,568–2,779 lines [K075]. Stage 1 hand-mirrors the core/dag walks [K002], and core composer modules import guided-named modules [K014]. Invariant 1 holds [X2b §B3].

### S11 — Composer tools

22,586 lines in 18 files, 42 tools [S11 §header][S11 §Baseline delta]. `tools/_common.py` is a 3,971-line module mixing many unrelated responsibilities, imported by all 7 tool planes, whose docstring's import surface is false [K079]. Deferred-blob classification misses markers in multi-query templates [K081], part of the multi-query cross-source cluster [X4 §E][05 §2.2]. Tool arguments are rejected, not coerced [S11 §Baseline delta]. The tool layer fabricates `on_error="discard"` at 5 production sites, which ADR-040 §3 bullet 2 forbids because the runtime has no default for that field; this is the already-tracked defect elspeth-0aace271b4 (dta-au/elspeth#171), and the claimed construction-boundary gap was refuted [K166].

### S12 — Composer governance

22,786 lines in 58 modules plus 1,716 lines of skills: advisor END gate, required controls, custody and redaction, proposal → commit, YAML, composer audit, the tutorial run [S12 §header]. Completion-gate reads use the moving head, so a recovery persist can erase a blocked advisor fact [K007], and a review-only save can orphan same-turn proposals [K008]. Required-control insertion is sanctioned but not ratified in an ADR or the AGENTS.md wording [K023][ruling 7]; the advisor END gate has no ADR [K082]. The tutorial run takes its three scrape fixtures from public GitHub Pages at runtime (`https://dta-au.github.io/elspeth`), so egress-restricted deployments cannot run the ADR-031 canary, the override cannot point at a private mirror because of the `public_only` SSRF gate, and a site change alters tutorial behaviour with no release; this has already broken every deployment once [K161].

### S13 — Guided lane (retirement inventory)

`composer/guided` is 21,460 lines in 20 files, and about 35K lines of guided backend exist in all [S13 §header][K013][X1 §7]. Freeform fork and revert run on the guided-operations ledger, so deleting guided by name breaks them [K012]; guided behaviour fills ≥ 41 % of `sessions/service.py` [K016]; `post_guided_respond` is a single 2,920-line function [K019]. The tutorial runs on the guided lane, but the ruling keeps that infrastructure, so retiring the mode is not blocked [K013][ruling 2]. ADR-031's canary premise ("the same guided machinery every user exercises") has been stale since the retirement and the ADR is unamended; freeform, now the only user authoring surface, has never had a non-adaptive fixed-script canary, a gap that predates the retirement but now sits on the primary surface [K160]. Diagram 03 §8 maps the guided lane's coupling to freeform and the tutorial [03 §8].

### S14 — Sessions domain

32,480 lines in 27 files; the Sessions DB is 47 tables in 10 domains at epoch 66 [S14 §header][S14 §Baseline delta]. `SessionServiceImpl` is a 10,546-line, 172-method god object on three transaction-ownership models [K083]. Session-table DML is split with `coordination/repository.py` [K034], and archive quarantine needs Linux `renameat2`, unverified on the EFS/NFS mounts both clouds use [K084].

### S15 — Sessions HTTP routes

23,867 lines in 24 files, of which about 10.6K are guided; 61 routes in 7 routers [S15 §header][S15 §Baseline delta]. `send_message` and `recompose` are ~82 % duplicated and have drifted [K085]; the YAML export route runs a lock-taking transaction on the event loop [K086]; `resolve_interpretation` turns a Tier-1 `AuditIntegrityError` into an unlogged 500 [K088]. K010 (web-review R02) was refuted: epoch 66 made it unreachable [K010].

### S16 — Web execution

20,219 lines in 34 files: Stage 2 preflight, launch admission, one run worker per process, progress streaming and run recovery [S16 §header][S16 §Responsibility][S16 §Concurrency model]. `/execute` maps envelope refusals to 404 and two admission errors to 500 [K089]; the fan-out guard reads whole files on the event loop [K090]; web runs record the engine-default rate limit while the operator's governs [K003]. A PostgreSQL lock-order deadlock has no 40P01 handler [K005]. Each replica runs one pipeline at a time, and other users' runs queue FIFO with no bound, fairness, run-duration cap or queue signal; the design is deliberate, but the capacity limit is undocumented [K150]. Execution writes the ADR-034 inline-blob resolution provenance (blob id, pinned sha256, encoding, option path) only to a Sessions DB table that nothing reads, so the Landscape cannot show that a plugin option came from an inline blob [K155].

### S17 — Web coordination

16,504 lines in 27 files, about 20 authority classes, the fence protocol, membership and the run-start saga, with no ADR recording the design [S17 §header][S17 §Baseline delta]. Peer compatibility columns are written and never read for a peer check; only a `coordination_protocol` bump without a schema change is unguarded [K093]. There are at least seven Sessions-DB clock implementations with divergent handling [K094]; a run cancelled after permit with failed output finalisation is invisible to recovery [K006].

### S18 — Web app composition, auth and identity

20,922 lines in 46 files, with four IdP profiles plus local auth and three auth stores [S18 §header][S18 §Authentication model: three stores, one token]. Synchronous Landscape auth-audit writes run on the event loop [K096], and worker-pool saturation on the auth path returns 500 [K097]. Uvicorn behind nginx collapses all clients into one rate-limit bucket [K004]; `auth.db` stays local SQLite even in external-PostgreSQL mode [K098].

### S19 — Web supporting domains

15,823 lines in 36 files: blobs, plugin policy, catalog, shareable reviews, audit readiness [S19 §header]. Share-link recipient views are not audited [K102]; the credential regex rejects ordinary dotted identifiers [K101]; `blobs_table` has two writer packages [K100]. The catalog's "future microservice" protocol is in practice an in-process library cycled with plugin policy [S19 §Baseline delta].

### S20 — Deployment and operations

3,512 lines of runtime deployment modules and 19,900 lines of acceptance harness [S20 §header]. **High:** `elspeth web` replaces an env-configured SSO provider with local auth [K103]. The ACA probe cannot qualify production's 2/4 topology [K105]; PostgreSQL TLS is a contract only on AWS [K106]; production ECS boot depends on a private acceptance module [K104].

### S21 — Frontend data layer

22,894 production lines and 30,843 test lines: 12 Zustand stores, 85+ client endpoints, a WebSocket protocol and a cross-store bus [S21 §Baseline delta]. The Tier-3 boundary uses unchecked casts at 52 client sites plus ~33 identity endpoints, and the strict `CompositionState` decoder is bypassed on three write paths [K033]. Cross-tab logout is broken [K107]; guided-named modules are shared infrastructure [K015].

### S22 — Frontend chat and tutorial

64,663 lines in 146 files, 20,745 of them production TS/TSX [S22 §header]. `ChatPanel` is a 3,094-line function fusing 8+ responsibilities [K108]; a multi-query prompt review with no node-level template cannot be approved [K109]. The tutorial's frontend script branches are 27 real `isTutorial` conditionals in 8 files, left for a maintainer ruling [K139]. Guided-replay dedupe matches user messages on trimmed content, so after a declined guided revision or correction (a guided user turn with no `chat_messages` twin) an identical freeform re-send is hidden from the rendered transcript; the server record and the audit trail are unaffected, and reach is limited to terminal guided sessions [K162].

### S23 — Frontend workspace shell

100,417 lines in 333 files, production and test combined (the frontend-wide split is 69,903 production / 132,755 test), with no component, state or routing description in the baseline [S23 §Baseline delta][S23 §header][critic G1]. The frontend has no internal layering and no boundary rule [K110]; browser E2E never drives the composer LLM loop [K030]; ESLint and stylelint run in no CI job or hook [K112]; one required E2E spec asserts a control never rendered on an empty session [K113]. In the People & Access panel, "Finish removing" for a half-deleted local account lives only in component state, so a reload or re-select loses it and a new code comment claims the opposite; the server supports the recovery, and a later registration of that username would inherit the live identity's roles [K182].

### S24 — Enforcement architecture

44,283 non-fixture lines of `elspeth-lints` with 24 rules in 10 families (7 `Category` values), plus gate scripts, CI and allowlists [S24 §header][S24 §Baseline delta][K135][critic G12]. Four CI- or pre-commit-wired gate scripts and one workflow were not assessed (§3.6) [critic G7]. The static-analysis job stops at a non-deliberate immutability red and skips 9 gating steps; of 3 red steps besides trust-tier only the fingerprint mismatch is real drift [K116]. Trust-boundary rules and the parity harness never run in CI [K117], three jobs are skipped outright [K118], and the layer rule is never evaluated on push [K119].

### S25 — Satellites

`gateway/` is 10,498 `.py` lines (3,230 `src` production, 5,757 `tests/`, 1,511 conformance/mock/scaffold) [critic-2 N1] and standalone in both directions [00 §Measured size][critic G1]; `evals/` is 7,028 [S25 §header][S25 §Baseline delta]. **High:** the ECS Scenario C sidecar is given 7 of 13 required env names and cannot start [K123]. The conformance kit cannot qualify a real derived adapter [K124], and no CI lint or type gate covers `gateway/` or `evals/`, where strict mypy already finds 56 errors in gateway [K127].

---

## 7. Conclusions and recommendations

### 7.1 Conclusions

1. **The engine and audit core are sound in design and conform in code where measured; its defects are specific, fixable and mostly missed siblings.** K051 repeats an already-fixed aggregation defect [K051]; K052 and K132 are collector gaps next to working aggregation paths [K052][K132].
2. **The web tier is where size, coupling and defects concentrate, and it is where the baseline is silent.** It is 53 % of production Python [01 §2], and 48 of the 84 C1-round Mediums have at least one member row in the web slices S10–S23, 41 of them only there [measured: python3 over temp/verified-concerns.json, cid < K149, Medium clusters with members in S10–S23 → 48, all members in S10–S23 → 41]. The R2 members carry gap ids (D02, G9-R19 …), not slice ids, so that instrument cannot see them; read by claim, 7 of the 8 R2 Mediums concern the web tier or its frontend (K150, K155, K160, K161, K162, K166, K182) and one, the PostgreSQL version check, does not (K154) [K150][K154][K155][K160][K161][K162][K166][K182].
3. **The structural risk is about the future, not the present.** Every boundary holds today [K024]; with more developers arriving, the missing gates and degraded CI signal are what would let it erode [ruling 6][K116][K119].
4. **Guided retirement and structural decomposition are the same programme at different depths.** Deleting the guided lane does not by itself break any package cycle, but it removes `composer.state`'s guided import, which X1 names as the first step of extracting the web domain model [X1 §7]; guided code also sits inside the same god-files that decomposition must split [K016].
5. **Several items are decisions, not fixes.** Replay/verify [K056], the required-control ratification [K023], the tutorial's frontend branches [K139] and the supported multi-replica statement [S17 §Baseline delta] need maintainer rulings before any code moves [ruling 4][ruling 7].

### 7.2 Recommendations: the 06 programmes

The architect handover groups the verified clusters (all 148 C1 clusters at C2; for K149–K182 see 06's own post-critic revision) into seven programmes in dependency order, lists 15 decisions and proposes ten first tickets; no tickets were created [06 §0][06 §2][06 §3][06 §5]. Pointers:

| Programme | Scope | Key clusters | Pointer |
|---|---|---|---|
| **P1** Guided retirement, tutorial kept | extract the shared ledger and misfiled symbols, replace the freeform `/guided` probe, then delete | [K012][K013][K014][K015][K016][K017][K019][K020] | [06 §2.1] |
| **P2** Supported-deployment and runtime defects | the five Highs outside P3, plus Mediums that break or degrade a supported deployment or run | [K103][K123][K051][K063][K062] | [06 §2.2] |
| **P3** Wire-or-remove decisions | replay/verify and other declared-but-dead features; silent deletion not recommended | [K056][K053][K065][K093] | [06 §2.3][ruling 4] |
| **P4** Multi-developer readiness | module-acyclicity gate (X1 R1), web role-layer rule (R2), layer-model adjudication (R10), restoring CI signal, governance | [K024][K116][K117][K118][K119][K031][K030] | [06 §2.4][X1 §7] |
| **P5** Structural decomposition | after P1 and the gates: split `sessions.protocol`, session store layer, extract the web domain model | [K002][K034][K083][K074][K142] | [06 §2.5][X1 §7] |
| **P6** Audit-integrity hardening | DB CHECKs for terminal invariants, export completeness, collector dispatch, follower equivalence | [K026][K040][K132][K047][K048] | [06 §2.6] |
| **P7** Documentation truth | ARCHITECTURE.md refresh from the 264 slice delta rows (+~75 ADR rows) [critic G2]; ADR amendments and ratifications | [K027][K028][K029][K023][K082] | [06 §2.7][00 §Execution Log] |

**Order.** Open P2 and the P4 gates in parallel, then P1, then P5; P3, P6 and P7 run alongside once their decisions are made [06 §0]. X1's own order is gates first (R1, R2), decisions next (R10), then small seams (R6–R8), then `sessions.protocol` (R5) and the session store (R4), and the domain-model extraction (R3) with or after guided retirement (R9) [X1 §7]. **Do not target "web package SCC = 0"**: it stays at 15 or more buckets even after 134 modules move; the measurable targets are module acyclicity held at 0, role inversions trending to 0 and lazy cycle-breakers trending from 41 to 0 [X1 §7].

**Decisions to take first** (06's register, D1–D15) [06 §3]: wire or remove replay/verify (D1) [K056]; the tutorial's ADR-031 Stage 3 (D2) [K013]; the frontend tutorial branches under invariant 2 (D3) [K139]; ratifying required-control insertion (D4) [K023]; plugins-above-engine vs peers (D6) [X1 §1.2]; the supported multi-replica and ACA statement (D13) [K105]; and re-prioritising the K062 tracker row from P3 to match its verified High (D14) [K062][05 §7].

**First tickets** in 06 start with the failing required E2E spec, then the five P2 Highs (K103, K123, K051, K063 and the Jinja bound for K062), the R1 and R2 gates, the Testcontainer timeout and the CI-signal restoration [06 §5][K113][K103][K123][K051][K063][K062][K147][K116][K117].

---

## 8. Appendix

### 8.1 Workspace file index

| File | What it is | Authority | Source |
|---|---|---|---|
| `00-coordination.md` | scope, pin, slice map, agent counts, execution log | method record | [00 §Analysis Configuration] |
| `01-discovery-findings.md` | holistic discovery; §8.2 corrected by X1; validated with corrections | measured sizes and import structure | [01 §Validation corrections] |
| `02-subsystem-catalog.md` | 25 validated slice entries (~11.6K lines) with a row → cluster cross-reference | per-slice detail, ±15 % residual on unvalidated claims | [02 §header][00 §Execution Log] |
| `03-diagrams.md` | 20 indexed diagrams, 19 Mermaid blocks plus one table (C4 L1–L3, dependency, sequences, state machines, deployment, guided map), validated | pictures of this report's §3 | [03 §9] |
| `04-final-report.md` | this report | synthesis | [00 §Analysis Configuration] |
| `05-quality-assessment.md` | dimension ratings, the non-refuted clusters by theme (146 at C2; 177 after R2), hotspots, ADR scorecard, validated | quality synthesis | [05 §1][05 §Validation corrections][measured: arithmetic 148 − 2 refuted = 146; 182 − 5 refuted = 177] |
| `06-architect-handover.md` | codebase map, programmes P1–P7, decisions D1–D15, risk register, first tickets | planning | [06 §0] |
| `temp/verified-concerns.md` / `.json` | 182 clusters (148 from C1, K149–K182 from the R2 gap round with basis "R2 single neutral verifier"): verified claims, final severities (High 6 / Medium 92 / Low 79 / refuted 5), provenance | **sole authority for severity and wording** | [VC §Method][VC §Outcome] |
| `temp/verify-input.json` | the 179 C1 verification units (16 High + 128 Medium rows + 35 cross-cut gap sections) | extractor output | [00 §Execution Log] |
| `temp/baseline-deltas.md` | 264 slice delta rows + ~75 ADR conformance rows (X2a ~51, X2b ~24) | intended vs built; rows extracted, not verified | [measured: "Delta rows" column sum → 264][critic G2] |
| `temp/slice-S01…S25-*.md` | validated slice entries (concatenated into 02) | per-slice source | [00 §Execution Log] |
| `temp/slice-summaries.md` | pre-validation navigation index | navigation only; entries and VC take precedence | [00 §Execution Log] |
| `temp/cross-X1-dependencies-layering.md` | measured module graph, SCCs, role layering, lazy imports, R1–R11 seams | layering authority | [X1 §0] |
| `temp/cross-X2a-…`, `temp/cross-X2b-…` | ADR-001–024 and ADR-025–049 conformance, system invariants | ADR conformance | [X2a §1][X2b §Part A] |
| `temp/cross-X3-test-architecture.md` | pyramid, gates, CI selections, gaps | test architecture | [X3 §1.1] |
| `temp/cross-X4-existing-evidence.md` | web review, tracker, retired code index, governance consolidation | prior evidence | [X4 §1] |
| `temp/import-matrix.md` | mechanical package import matrix and SCCs | seed for X1 | [00 §Execution Log] |
| `temp/validation-*.md` | validation reports for 01 (`validation-01-discovery.md`), G1–G5 (the 02 gate), 03 (`validation-03-diagrams.md`), 04 (`validation-04-report.md`), 05 (`validation-05-quality.md`) and 06 (`validation-06-handover.md`), plus the post-critic re-validations `validation-R2-03-diagrams.md` and `validation-R2-04-report.md` (and any `validation-R2-*` files for 05 and 06) | validation record | [00 §Execution Log][measured: ls temp/validation-*] |
| `temp/validation-completeness-critic.md` | the whole-set completeness critic: 18 gaps G1–G18 (1 High, 7 Medium, 10 Low), coverage instrument and controls | drove the R2 gap round and this report's R2 revision | [critic §3][00 §Execution Log] |
| R2 gap round (`wf_8c2f2711-89f`) | 37 agents; its output is folded into `temp/verified-concerns.*` as K149–K182, with no separate artefact | verification record | [VC §Method][00 §Execution Log] |

### 8.2 How to re-run or verify

- **Recreate the pin.** Read only a detached worktree at `85ebf2739`; check `git -C <pin> rev-parse --short HEAD` and that `git -C <pin> status --short` prints nothing [00 §Analysis Configuration][measured: git -C <pin> rev-parse --short HEAD → 85ebf2739].
- **Sizes.** `find src/elspeth -name '*.py' -not -path '*/__pycache__/*' -not -path '*/frontend/*' -print0 | xargs -0 cat | wc -l` (production Python, 489,459 at the pin) and the same over `src/elspeth/web` (259,872) [01 §Validation corrections][measured: same commands at the pin].
- **Concern counts.** `python3 -c "import json,collections; print(collections.Counter(c['final_severity'] for c in json.load(open('temp/verified-concerns.json'))))"` must give High 6, Medium 92, Low 79, None 5 (182 clusters) [measured: that command → Medium 92, Low 79, High 6, None 5]. Before R2 it gave Medium 84, Low 56, High 6, None 2 over 148 clusters.
- **Frontend and gateway size.** Split `git ls-files` output by the `.test.`/`.spec.` suffix or a `tests?/`, `__tests__/` or `e2e/` directory; production and test must be reported separately (frontend 69,903 / 132,755) [measured: see §1][critic G1].
- **Module acyclicity.** X1's instrument is Tarjan over module-level imports, including implicit `__init__` edges, cross-checked by two implementations and a one-edge mutation that must produce a 25-module SCC; re-run it with that control before trusting a 0 [X1 §2.1][X1 §0].
- **Whether a finding still holds.** Every C1 cluster was UNCHANGED against `git log 85ebf2739..release/0.8.1` = 0 commits at C1 verification time [VC §Outcome]. One commit, `e69498f6c` (advisor checkpoint envelope parsing), landed on `release/0.8.1` afterwards; critic-2 checked that it touches no code any cluster cites [critic-2 N7]. Before acting on one, re-run `git log 85ebf2739..<branch> -- <cluster files>` and re-check the verified claim against the live tree.
- **Catalog-only claims.** Re-measure any catalog count, line citation or size not tagged to a K-id or `[measured]` before relying on it [02 §header][00 §Execution Log]; re-verify any catalog-only Low row before ticketing it [06 §4].
- **Probes against the pin.** Run live probes with `PYTHONPATH=<pin>/src` and confirm `elspeth.__file__` resolves inside the pin, as the S08 registry probe did; otherwise the probe measures the editable install in the main checkout [S08 §header].

---

## Validation corrections

Independent validation, 2026-09-23, against the workspace sources and the pin `85ebf2739`. Full report: `temp/validation-04-report.md`. Each change below replaced a claim whose tag pointed at a source that says something different. No severity was changed.

| # | Location | Was | Now | Source that decided it |
|---|---|---|---|---|
| V1 | §2.2 Verification totals | "one reported Medium (K062) ended at High [VC §Outcome][05 §1]" | same claim, plus a `[measured]` tag recording that VC's prose says two while its transitions table and the JSON give one | [measured: Counter over (reported, final_severity) → ('Medium','High') 1] |
| V2 | §3.5 Deployment topology | "three sources state differently [S20 §Deployment view]" | the disagreement is tagged to the entry that records it | [S17 §Baseline delta]; S20's entry has no such text |
| V3 | §3.6 Tier 1 | three Tier-1 reads "still apply defaults or coercion" [K140] | X2b sampled three; verification kept only the MCP `list_collisions` `union_field_origins` read as a real Tier-1 leniency | [K140] verified claim |
| V4 | §4.3 ADR status and prose | "ADR-048 still says 'Proposed' for a gate-green decision [K028]" listed as a thing the baseline states wrongly | "Proposed" is not drift (Task 8B not landed); the residual is 0.8.1 release-notes silence | [K028] verified claim |
| V5 | §4.3 ADR status and prose | "ADR-026/029/035/037/038 carry superseded prose [K029]" | 026/029/035/038 carry superseded prose; ADR-037's issue is an unrecorded assessment of its sixth enum member | [K029] verified claim ("the 'five members' sentence in ADR-037 is dated history and should not count as drift") |
| V6 | §6 S02 | "the intra-core checkpoint ↔ landscape cycle is a latent import-order hazard [K025]" | the hazard was not demonstrated at the pin; structural hygiene, not a defect | [K025] verified claim |
| V7 | §6 S10 | "`composer/service.py` … holds functions of 1,568–2,779 lines [K075]" | those functions are in `state.py`, `tool_batch.py` and `pipeline_planner.py`; `service.py`'s are 639 and 482 lines | [K075] verified claim |
| V8 | §6 S11 | "`tools/_common.py` is a 3,971-line sink of at least 8 responsibilities" | "mixing many unrelated responsibilities, imported by all 7 tool planes"; "at least 8" is the cluster *title*, not the verified claim | [K079] verified claim |
| V9 | §5.2 Governance | "no PR process at the time of X4" | "no PR process documented in CONTRIBUTING", with the `main` ruleset's PR requirement stated | [X4 §F] |

---

## Revision R2 (post-critic)

Revision of 2026-09-24 against the completeness critic (`temp/validation-completeness-critic.md`) and the R2 gap round. Every severity comes from `temp/verified-concerns.json` (182 clusters: High 6, Medium 92, Low 79, refuted 5). No High was added, removed or re-rated. The frontend split, the gateway split, the delta-row count and the gate-script wire sites were re-measured at the pin for this revision; the controls are in the tags.

| # | Gap / cluster | Location | Change |
|---|---|---|---|
| R2-1 | G1 | §1 "How big it is" | Frontend restated as 69,903 production TS/TSX lines (259 files) plus 132,755 test lines (338 files), re-measured with a control; gateway given as 3,871 production + 6,627 test |
| R2-2 | G1 | §1 "Centre of mass" | "plus all of the TypeScript" → "plus the 69,903 lines of production TypeScript"; added that the production frontend is ~14 % of production Python, not the ~41 % the all-files total implies |
| R2-3 | G1 | §2.3, §3.3, §4.2, §4.4, §6 S23, §6 S25, §8.2 | every frontend size now uses the production/test split; S23's 100,417 marked as production and test combined; the gateway split added to S25; a split rule added to §8.2 |
| R2-4 | G2 | §1 conclusion 6, §2.2, §4 intro, §7.2 P7 row, §8.1 | 275 → 264 slice delta rows (+~75 ADR rows), tagged `[measured]` with two independent counts |
| R2-5 | G5 | §2.3 | New read-depth bullet for S24, S20, S06, S03 and S13, taken from each slice's §Confidence |
| R2-6 | G5 | §3.6 Enforcement | "enforces" → "registers" for the lint rules, with the disclosure that 19 of 24 rule implementations were read at metadata level only |
| R2-7 | G17 | §2.3 | New bullet: the frontend and gateway test trees were examined only for structure and counts |
| R2-8 | G7 | §2.3, §3.6, §6 S24 | Named the unassessed CI/pre-commit gates, with wire sites checked at the pin: `scripts/state_engine_plugin_matrix.py` (`ci.yaml:1027`), `scripts/cicd/check_redaction_direction.py` and `scripts/cicd/assert_redaction_label.py` (`composer-redaction-gate.yml:77,95`), `scripts/git-hooks/pre-commit-secret-scan.sh` (`.pre-commit-config.yaml:63`) and the workflow `enforce-telemetry-backfill-trailer.yaml` |
| R2-9 | G12 | §6 S24 | "24 rules in 7 categories" → "24 rules in 10 families (7 `Category` values)" [K135] |
| R2-10 | G14 | §8.1 | Index completed: `validation-04-report.md`, `validation-06-handover.md` (and the other validation files by name), `validation-completeness-critic.md`, `verify-input.json`, verified-concerns at 182 clusters, baseline-deltas at 264, and the R2 round (folded into verified-concerns, no separate artefact). 05's cluster count is given as "146 at C2; 177 after R2" |
| R2-11 | G15 | §2.1 | New scope-exclusions line: `design/`, `tools/pdf`, `config/mcp` (the only part of `config/` beyond `config/cicd`), `docs-archive/`, and the unnamed deploy and example scripts, with file counts measured at the pin |
| R2-12 | R2 method | header source tags, §2.2 table and totals, §2.3, §5.1, §8.2 | Added the critic and R2 rows to the method table; totals now 182 clusters, 249 verification agents, votes 168/52/6, provenance by prefix NEW 99 / TRACKED 31 / REPORTED-AND-TRACKED 16 / PREVIOUSLY-REPORTED 36; 294 agents and ~37.6M subagent tokens before this revision; five refuted clusters named (K010, K045, K163, K180, K181); the `[critic Gn]` tag defined; the §8.2 re-run expectation corrected to 6/92/79/5 |
| R2-13 | G4, G8 | §2.3, §4 intro | Disclosed that the catalog Low rows and the baseline-delta rows were extracted, not verified as a set, and that R2 verified only the defect-shaped and recurring items the critic found |
| R2-14 | K150 | §3.5, §6 S16 | Per-replica run serialisation with an unbounded, unfair FIFO queue and no run-duration cap (Medium) |
| R2-15 | K154 | §3.4, §6 S03 | No runtime PostgreSQL version check; ACA defaults to 17 while every proof runs on 16 (Medium); the previously untagged §3.4 claim now carries the K-id |
| R2-16 | K155 | §3.4, §4.4, §6 S16 | ADR-034 inline-blob resolution provenance is kept only in a write-only Sessions DB table, outside the Landscape (Medium) |
| R2-17 | K160, K161 | §4.4 ADR-031 row, §6 S13, §6 S12 | ADR-031 canary premise stale and unamended, freeform without a fixed-script canary (K160, Medium); tutorial fixtures fetched at runtime from public GitHub Pages (K161, Medium) |
| R2-18 | K162 | §6 S22 | Guided-replay dedupe can hide an identical freeform re-send (Medium) |
| R2-19 | K166 | §6 S11 | Tool layer fabricates `on_error="discard"` against ADR-040 §3 bullet 2; the construction-boundary half is refuted (Medium) |
| R2-20 | K182 | §6 S23 | People & Access "Finish removing" recovery lost on reload (Medium) |
| R2-21 | K170 (G3) | §3.6 | New bullet on LLM tracing content egress: Langfuse half confirmed as operator-opt-in and web-denied; `azure_ai` half does not reach App Insights (Low). The critic's "content recording defaults on" framing was not used, because the verified claim refutes it for the reachable path |
| R2-22 | K093, K152 | §3.5 Deployment topology | "peer compatibility columns are written and never read" narrowed to match K093's verified claim (the same overstatement the critic raised for 03 as G11); added K152 (Low), no refusal of a second active replica on single-replica targets |
| R2-23 | R2 Mediums | §7.1 conclusion 2 | "48 of the 84" scoped to the C1 round, with the instrument's limit stated, and the 8 R2 Mediums classified by claim (7 web, K154 not) |
| R2-24 | — | §7.2 intro | 06's programme grouping scoped to the 148 C1 clusters, with a pointer to 06's own revision for K149–K182 |

---

## Validation corrections (R2)

Independent re-validation, 2026-09-24, of the R2 revision above, against `temp/verified-concerns.json` (182 clusters), the critic report and the pin `85ebf2739`. Full report: `temp/validation-R2-04-report.md`. No severity was changed, and no R2 revision row was reverted.

| # | Location | Was | Now | Source that decided it |
|---|---|---|---|---|
| RV1 | §4.4 ADR-031 row, "Why it matters" | "the canary covers machinery only the tutorial uses" | the canary's guided planner surface is now driven only by the tutorial, although it still exercises the shared planner, commit and validation code | [K160] verified claim ("The tutorial still runs the shared planner, commit and validation code, so it has not lost all value for freeform") |
| RV2 | §6 S17 | "Peer compatibility columns are written and never read" | "… never read for a peer check" | [K093] verified claim ("No production code reads them to check a peer"); the same overstatement the critic raised for 03 as G11 |
| RV3 | §3.6 Enforcement | the telemetry-backfill workflow "never assessed by any slice" (unqualified) | adds that S24 read only its trigger | [S24 §Confidence] ("the other 6 workflows (triggers only)"; the telemetry workflow is one of those six at the pin) |
| RV4 | §8.1 `temp/validation-*.md` row | index named no R2 re-validation report, although `validation-R2-03-diagrams.md` already existed | names `validation-R2-03-diagrams.md`, `validation-R2-04-report.md` and any R2 files for 05/06 | [measured: ls temp/validation-R2* → validation-R2-03-diagrams.md]; G14 |
| RV5 | §8.1 05 row | "146 at C2; 177 after R2" tagged only to 05, which never prints either number | same figures, with an arithmetic tag | [measured: 148 − 2 = 146; 182 − 5 = 177] |
