# 05 — Quality Assessment (code and architecture, as built)

**Pinned tree:** `release/0.8.1` @ `85ebf2739`, read from the detached worktree `.claude/worktrees/arch-analysis-pin` [measured: git -C <pin> rev-parse --short HEAD → 85ebf2739; git status --short → 0 lines].
**Baseline:** `ARCHITECTURE.md` (last updated 2026-09-11) and the ADR set under `docs/architecture/adr/` [measured: grep -n 'Last Updated' <pin>/ARCHITECTURE.md → line 5, 2026-09-11].
**Date:** 2026-09-23, revised 2026-09-24 (see "Revision R2" at the end). This document synthesises the workspace; it adds no new review and re-rates no concern [00 §Execution Log].

**How to read the source tags:**

- `[K0NN]` is a cluster in `temp/verified-concerns.md`, the sole authority for severity and wording; the text quoted or paraphrased is that cluster's *verified claim*, never the slice row's original wording [VC §Method].
- `[S07 §Complexity]` is a section of a catalog entry in `02-subsystem-catalog.md`. `§Complexity` abbreviates the entry's "Complexity & tech-debt hotspots" section, `§Test map` its "Test map" section, and so on.
- `[X1 §2.7]`, `[X2a §2]`, `[X2b §Part A]`, `[X3 §6]`, `[X4 §E]` are sections of the cross-cut files in `temp/`.
- `[01 §N]` is `01-discovery-findings.md`; `[00 §Execution Log]` is `00-coordination.md`; `[VC §Outcome]` / `[VC §Method]` are the header sections of `temp/verified-concerns.md`.
- `[measured: …]` is a command this assessment ran itself at the pin, with the command named.

The dimension ratings in §1 are this document's own synthesis over the verified concerns and the cross-cuts. They are not concern severities, and they do not change any concern's severity [VC §Method].

---

## 1. Assessment summary

**Calibration.** Adversarial verification produced 0 Critical, 6 High, 92 Medium, 79 Low and 5 Refuted clusters out of 182 [VC §Outcome]. 148 of them came from the first verification pass (C1 plus the severity tiebreak). The other 34 (K149–K182: 8 Medium, 23 Low, 3 refuted) came from the R2 gap round, which checked items the completeness critic found the extractor could not reach [VC §Method][00 §Execution Log]. 9 of the 14 clusters reported as High ended below High, and one reported Medium ended at High [VC §Outcome][K062]. (VC's prose says two; its own transitions table and the JSON give one, K062; K056 was reported High with a split vote [VC §Outcome][K056].) Of the 182 clusters, 99 are NEW to this analysis, 31 were already TRACKED, 16 were REPORTED-AND-TRACKED and 36 were PREVIOUSLY-REPORTED [measured: python3 Counter over the leading label of each `provenance` string in temp/verified-concerns.json]. The R2 clusters carry qualified provenance strings, for example "TRACKED(…) … is NEW" on K162. The count above classifies each one by its leading label, which is why VC §Outcome's exact-string tally shows NEW 95. All 182 are unchanged after the pin, because `git log 85ebf2739..release/0.8.1` was empty when they were checked [VC §Outcome].

| Dimension | Rating | Evidence basis (one line) |
|---|---|---|
| Audit integrity and correctness | **Adequate** | The terminal-outcome model, the DB-clock authority and the token-fenced mutation APIs conform in code [X2a §1][X2b §Part A], but crash recovery aborts on a resumed gate [K051], the "complete" export omits whole ledgers [K040], 11 batch plugins abort a run on one bad row [K063], and inline-blob resolution provenance lives only in the Sessions DB, not in the Landscape [K155]. |
| Security and trust boundaries | **Weak** | Two verified Highs sit here: unbounded Jinja evaluation on an authenticated web surface [K062] and `elspeth web` silently replacing SSO with local auth (with open registration on ECS upgrade mode) [K103]; the audit scrubber misses AWS and Anthropic keys [K035] and expression evaluation is unbounded at runtime [K037][K038]. |
| Architectural structure and coupling | **Adequate** | The import-time module graph is acyclic and `contracts` is a true leaf [X1 §0][X1 §1.2], but the web tier's vertical packages form one package-level SCC [X1 §2.5] and only `contracts < core < engine` is enforced [K024]. |
| Maintainability and complexity | **Weak** | The files ARCHITECTURE.md already flagged as large have kept growing [measured: wc -l at 85ebf2739][01 §8], with single functions of 2,000+ lines [K075][K019] and god objects of 5,000–10,500 lines [K083][K055]. |
| Testability and test architecture | **Adequate** | A bottom-heavy 2,380-file suite with 38 whole-tree gates and a mock-discipline baseline [X3 §1.1][X3 §3.2][X3 §8.2], but browser E2E never drives the LLM loop [K030] and whole selections never run in CI [K031]. |
| Operability and deployment | **Weak** | Two verified Highs make shipped deployments fail to start or boot insecurely [K103][K123]; the ACA probe cannot qualify the production topology [K105], PostgreSQL TLS is checked only on AWS [K106], nothing checks the PostgreSQL server version although the ACA bundle defaults to 17 and every proof runs on 16 [K154], and each web replica runs one pipeline at a time with an unbounded FIFO behind it [K150]. |
| Enforcement and governance (gates, lints, CI) | **Weak** | At the pin the static-analysis job stops at a *non*-deliberate red (immutability FG3) and so skips 9 later gating steps, including the deliberately red trust-tier step. Besides the trust-tier step there are 3 red gating steps, of which only one (the trust-boundary fingerprint mismatch) is real drift; the other two are lint false positives [K116]; three more jobs are skipped outright [K118], two rule sets never run in CI [K117], and on push the layer rule is never evaluated [K119]. |
| Documentation fidelity (ARCHITECTURE.md, ADRs vs code) | **Weak** | ARCHITECTURE.md omits `web/` (53 % of production Python) from its dependency graph [X1 §1.1][01 §2]; user docs present replay/verify as working when they are unwired [K056]; ADRs fare better, with 29 of 47 present ADRs conformant in code in X2's scoring, and 28 once the R2 finding against ADR-040 is applied (§6) [X2a §1][X2b §Part A][K166]. |

**Reading the enforcement row against maintainer context.** The trust-tier red is a deliberate fail-closed state, and this assessment does not count it as a defect [01 §7]. The finding is narrower. Because no step in static-analysis uses `continue-on-error`, the job halts at the first red, and at the pin the first red is the immutability gate, not the trust-tier step [K116]. The deliberate red has therefore become a place where drift accumulates unseen and gate signal is lost [K116][K117][K118].

**Effect of the R2 round on the ratings: none changed.** The 8 new Mediums add evidence to rows that already had it. K155 is one Medium against the Adequate audit row, which already carries a High (K051) and a Medium (K040) [K155]. K150 and K154 fall under Operability, already Weak [K150][K154]. K160 and K161 bear on the tutorial canary and ADR-031, which fall under Documentation fidelity and Operability, both already Weak [K160][K161]. K166 is a composer ADR-040 breach [K166], and K162 and K182 are frontend state defects [K162][K182]. None of these reaches a dimension that is currently rated Adequate or better strongly enough to move it.

---

## 2. Findings

### 2.1 The six High findings (verified claims in full)

#### K051 — GateExecutor opens NodeStateGuard at attempt=0 ignoring resume/claim attempt offsets; UNIQUE collision aborts crash recovery

| Final severity | Reported | Basis | Verdict | Claim type | Provenance | Member rows |
|---|---|---|---|---|---|---|
| **High** [K051] | High | agreeing votes | CONFIRMED | defect | NEW | S06:C1 |

**Verified claim** [K051]:

> GateExecutor.execute_config_gate opens its NodeStateGuard without `attempt` or `resume_checkpoint_id`, so it always writes at attempt 0 with no resume provenance, whatever the token's resume_attempt_offset is. When a resumed fork-child token re-runs a branch whose gate already recorded a node_state in run 1, the insert violates the node_states attempt uniqueness and Orchestrator.resume aborts. This was reproduced end to end at the pin. The analogous lease-recovered scheduler-claim path follows from the same code (the drain passes attempt_offset to transforms only), but it was not executed. [K051]

**Assessment.** Crash recovery is the property the Landscape exists to guarantee, and this defect makes resume abort on a legitimate replay [K051]. The same class of defect was already fixed for `AggregationExecutor` (closed `elspeth-262911c26b`), so it is a missed sibling rather than a design gap [K051]. The lease-recovered claim path follows from the same code but was not executed [K051].

#### K063 — 11 batch-aware plugins still raise TypeError on wrong-type row value, aborting the run (only batch_stats converted)

| Final severity | Reported | Basis | Verdict | Claim type | Provenance | Member rows |
|---|---|---|---|---|---|---|
| **High** [K063] | High | agreeing votes | CONFIRMED | defect | REPORTED-AND-TRACKED | S08:C2 |

**Verified claim** [K063]:

> At pin 85ebf2739, 11 batch-aware plugins still raise a bare TypeError when a buffered row has a value of the wrong type. They are the batch_* classifier_metrics, distribution_profile, drift_compare (2 sites), effect_size, experiment_compare, outlier_annotator, paired_preference, replicate, threshold_summary and top_k plugins, plus report_assemble. The aggregation executor records the node state as FAILED and then re-raises the error, so the whole run aborts: exit 4, a traceback, "0 failed", and on_error never fires. Only batch_stats converts the error through BatchRowTypeError into a routable batch error. [K063]

**Assessment.** A row-level data problem escalates to a run abort, the opposite of the row-routing contract [K063]. It is tracked as P1 `elspeth-5887fb7928` (fixing, blocked by `elspeth-d2e3f29d10`) [K063][X4 §E]; X4 recorded it as "16 plugins", and at the pin 11 still raise [K063][X4 §E].

#### K056 — run_mode replay/verify and replay_from validated but never consumed; CallReplayer/CallVerifier have no production consumers

| Final severity | Reported | Basis | Verdict | Claim type | Provenance | Member rows |
|---|---|---|---|---|---|---|
| **High** [K056] | High | severity tiebreak (neutral judge) | CONFIRMED_WITH_ADJUSTMENT | defect | NEW | S07:C-01 |

**Verified claim** [K056]:

> `ElspethSettings.run_mode` (live/replay/verify) and `replay_from` are declared at core/config.py:2077-2084. The only validation, at :2344-2351, checks that `replay_from` is present, and nothing checks that the named run exists. No production code reads either field after that. The documentation still presents both modes as working features: docs/reference/configuration.md:68-69 and its "Run Modes" table at :91-97 say replay "Use recorded responses from a previous run" and verify "Compare new results against a previous run", with no "not implemented" caveat. No ADR defers the modes. The config-alignment test marks unwired neighbours "(PENDING - not wired)" but gives RUN_MODE_FIELDS no such marker (tests/unit/core/test_config_alignment.py:438-447). [K056]
>
> As a result, a YAML/CLI pipeline with `run_mode: replay` or `run_mode: verify` loads cleanly, even when `replay_from` names a nonexistent run, and executes as a live run. It makes real, billable external calls and writes to live sinks, with no error or warning. `CallReplayer` and `CallVerifier` are exported from plugins/infrastructure/clients/__init__.py:61-77, but nothing constructs them, and discovery.py:54-55 excludes their modules. [K056]
>
> Adjustments: (a) Reach. The web composer drops both keys at import (web/composer/yaml_importer.py:117,119), so only the YAML/CLI authoring surface is affected. AGENTS.md names that as one of the two first-class surfaces. (b) Provenance. `settings_json` records `run_mode: replay`, which truthfully reflects the configured settings. The calls themselves are recorded as live. This misleads a later reader but is not audit-integrity loss, which is why the finding is not Critical. (c) Doc drift. The RunMode docstring (contracts/enums.py:374) cites a `runs.run_mode` column that does not exist; landscape/schema.py has no run_mode column. [K056]

**Assessment.** This is unwired intent, not dead code: under the maintainer's ruling it needs a wire-or-remove *decision*, and silent deletion would lose the intent while leaving the documentation false [K056]. Reach is the YAML/CLI surface only, because the web composer drops both keys at import [K056]. It is not Critical because `settings_json` truthfully records the configured mode, although the calls are recorded as live [K056].

#### K062 — Jinja rendering has no CPU/memory bound while web planner/users author templates

| Final severity | Reported | Basis | Verdict | Claim type | Provenance | Member rows |
|---|---|---|---|---|---|---|
| **High** [K062] | Medium | severity tiebreak (neutral judge) | CONFIRMED_WITH_ADJUSTMENT | defect | TRACKED | S07:C-07 |

**Verified claim** [K062]:

> The shared Jinja2 sandbox (plugins/infrastructure/templates.py:36-39) has no CPU or memory bound. Its docstring justifies that by saying templates are "trusted config, not end users" (templates.py:6-8), and that premise is false on the supported multi-user web surface. There, any authenticated session owner supplies prompt_template, either through POST /{session_id}/state/yaml (routes/composer/state.py:852-862) or by steering the planner. The worst path is at compile time, not in render loops. Jinja constant-folds when it compiles, so the LLMConfig.prompt_template field validator (llm/base.py:416-426 -> PromptTemplate -> env.from_string, llm/templates.py:113) evaluates `a ** b` inside the web process during composer validation or import. The validator runs even when the rest of the config is invalid. A single large integer power holds the GIL for its whole duration, and the enforced one-process-per-container deployment (app.py:1890-1921) means that one small authenticated request stalls the entire replica for an unbounded time, including the event loop, the timeout wrappers and the health probes. At render time, string multiplication and nested loops are also unbounded on the single in-process execution worker (execution/service.py ThreadPoolExecutor(max_workers=1)). The gap is already tracked as elspeth-bbc7000e61, which the slice mislabelled as NEW. [K062]

**Assessment.** The sandbox's own docstring rests on a premise ("trusted config, not end users") that the multi-user web surface falsifies [K062]. The worst path runs at compile time inside the web process, and one process per container means one small authenticated request can stall a replica [K062]. The tracker holds this as an open **P3** (`elspeth-bbc7000e61`) while verification rates it High, a priority mismatch the tracker should absorb [K062]. The sibling bounds on the expression parser are open Mediums [K037][K038].

#### K103 — elspeth web overwrites ELSPETH_WEB__AUTH_PROVIDER with --auth default 'local', so every shipped SSO deployment refuses to boot

| Final severity | Reported | Basis | Verdict | Claim type | Provenance | Member rows |
|---|---|---|---|---|---|---|
| **High** [K103] | High | agreeing votes | CONFIRMED_WITH_ADJUSTMENT | defect | NEW | S20:C1 |

**Verified claim** [K103]:

> `elspeth web` always overwrites ELSPETH_WEB__AUTH_PROVIDER with its `--auth` value, and the default is "local" (cli.py:4747, 4789; there is no envvar binding and no auto_envvar_prefix). None of the shipped launchers passes `--auth`, so any SSO provider configured only through the environment is replaced by "local". The effect depends on the provider. For ACA production (entra), boot fails closed with "Local auth does not use entra_tenant_id" and the container crash-loops while the doctor passes, as the member claims. For ECS upgrade mode (oidc), the member's "failure is closed / no silent downgrade" is WRONG: the task boots silently on LOCAL auth with registration_mode=open behind an internet-facing ALB, and readiness reports "local authentication configured". Systemd and compose ship no SSO env, so they are affected only when an operator adds SSO through the env file. [K103]

**Assessment.** The same launcher defect produces two different failures: a crash-loop that the doctor does not catch (ACA/entra), and a *silent downgrade* to local auth with open registration behind an internet-facing ALB (ECS/oidc upgrade mode) [K103]. The origin ticket was closed by the fix that introduced the unconditional environment write [K103].

#### K123 — Scenario C gateway sidecar cannot start: task definition supplies 7 of 13 required env names

| Final severity | Reported | Basis | Verdict | Claim type | Provenance | Member rows |
|---|---|---|---|---|---|---|
| **High** [K123] | High | agreeing votes | CONFIRMED | defect | NEW | S25:S25-C01 |

**Verified claim** [K123]:

> At the pin, the Scenario C task definition passes the gateway sidecar 4 environment variables (ADAPTER, UPSTREAM_ORIGIN, OAUTH_TOKEN_URL, MODEL_MAPPINGS) and 3 secrets (INBOUND_BEARER, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET). The gateway's `load_config` requires 13 names, so 6 are missing: OAUTH_AUTH_METHOD, MAX_MESSAGES, MAX_TOOLS, MAX_STRING_CHARS, MAX_SCHEMA_BYTES and MAX_SCHEMA_DEPTH. The uvicorn factory `build()` calls `load_config(os.environ)`, so it raises `ConfigError` at startup. The sidecar is essential, and the web container and the runtime doctor both wait for it to be HEALTHY, so the Scenario C service task and the runtime doctor cannot start. The only way around this is an operator-built derived image that bakes these six variables in with ENV. No deploy file, runbook or README tells the operator to do that: the README template bakes only ADAPTER. [K123]

**Assessment.** Because the web container and the runtime doctor both wait for the sidecar to be healthy, Scenario C cannot start at all without an operator-built derived image that no deploy file or runbook describes [K123].


### 2.2 Medium findings, grouped by theme

All 92 Medium clusters appear below, each exactly once, by theme rather than by slice [measured: python3 set check over temp/verified-concerns.json, 92 of 92 assigned; see Revision R2]. *Provenance* is the verification pass's classification: NEW (not previously recorded), TRACKED (a tracker row exists), PREVIOUSLY-REPORTED (in a dated review), REPORTED-AND-TRACKED (both) [VC §Method]. For the R2 clusters (K149 onward) the table shows only the leading label; the qualifiers, such as which part is new, are in VC [VC §Outcome]. *Slice(s)* are the cluster's member rows. For an R2 cluster that column gives the R2 unit id instead: D*nn* is a behavioural baseline-delta row, G9-R*nn* an orphan web-review Medium [VC §Method]. Titles are the cluster titles; where a title and its verified claim differ (for example K001, K103, K116, K139 and K144, whose claims narrow or correct the headline), the verified claim governs [VC §Outcome][K001][K103][K116][K139][K144].

#### M1. Audit trail and Landscape correctness (11)

Gaps between what the audit record says and what happened. The largest is an export that calls itself complete and omits whole ledgers [K040]; others record a rate limit that did not govern the run [K003], leave admin and share-link actions unaudited [K011][K102], let the collector closer skip the declaration contracts that aggregations run [K132], rely on Python alone for terminal-state rules the database could enforce [K026], and keep ADR-034 inline-blob resolution provenance in a Sessions DB table that nothing reads, not in the Landscape [K155].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K040] | 'Complete' audit export omits run_sources, coalesce/aggregation receipts, coordination/worker ledgers, preflight, run-start admission and several run-level columns | Medium | NEW | S03 |
| [K003] | Web runs record the engine-default rate_limit in Landscape/approval hash while operator execution_rate_limit governs [R12] | Medium | PREVIOUSLY-REPORTED | S02, S16 |
| [K011] | Local-account admin audit gaps: dev-admin deletion inheritance (R07), no auth_events row for credential create/reset (R08), actorless deletion row (R09); the closed hand-pinned auth_events vocabulary needs a Landscape epoch bump to fix | Medium | PREVIOUSLY-REPORTED | S03, S18 |
| [K102] | Share-link recipient views not audited; requesting_user_id unused; no recipient binding | Medium | NEW | S19 |
| [K132] | Collector closer runs no declaration-contract (val) dispatch unlike aggregation flush for the same batch plugins | Medium | NEW | X2a |
| [K026] | No DB-level CHECK constraints for terminal/status invariants (token_outcomes pair legality/completed XOR outcome, node_states.status, batches.status, operations.status); enforced only by Python on write/read | Medium | NEW | S01, S04 |
| [K043] | Journal postcommit work (BEGIN IMMEDIATE, fsyncs, DELETE) runs inside patched dialect.do_commit; AuditIntegrityError can surface as failed commit for durable data | Medium | NEW | S03 |
| [K044] | Barrier adoption and marker reset on token_work_items write no scheduler_events row, contradicting ADR-026 G29-closed | Medium | NEW | S04 |
| [K061] | External-call recording enforced by three different mechanisms; only automatic one is structural | Medium | NEW | S07 |
| [K088] | resolve_interpretation converts Tier-1 AuditIntegrityError into an unlogged coded 500 | Medium | NEW | S15 |
| [K155] | ADR-034 inline-blob resolution provenance is kept only in the web Sessions DB (a table nothing reads), not in the Landscape | Medium | NEW | R2: D07 |

#### M2. Engine: followers, recovery and retry semantics (6)

Multi-worker execution is not equivalent to leader execution: followers skip the expand-width fence and never retry, so a row's outcome depends on which worker claims it [K047][K048]. Two barrier paths leave recovery or group-failure semantics unimplemented [K052][K053], and a plugin's `retryable=True` error result is inert [K065].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K047] | Followers run without the max_expand_group_width fence (build_follower_processor passes settings=None) | Medium | NEW | S05 |
| [K048] | Followers never retry (no RetryManager built in FOLLOWER mode); row outcomes depend on which worker claims | Medium | NEW | S05 |
| [K049] | Every sink-bound token held in memory (pending_tokens) until the source loop ends | Medium | NEW | S05 |
| [K052] | Crash between collector flush and complete_barrier is unrecoverable (no residual receipts for collector) | Medium | TRACKED | S06 |
| [K053] | CollectorExecutor.notify_empty_group has no production caller; require_all empty_expansion verdict never rendered | Medium | NEW | S06 |
| [K065] | TransformResult.error(retryable=True) is inert; Textract transient/poll_timeout results never retried | Medium | NEW | S08 |

#### M3. Composer correctness, including the multi-query LLM contract cluster (14)

The multi-query LLM contract problems appear in three slices and in the web review and P1 tracker at once, the strongest cross-source signal in the existing evidence [X4 §E][K002][K081][K109]; K064 (R64) is an adjacent multi-query defect [K064]. Around it sit Stage-1/runtime validation mirrored by hand [K002], save paths that move the head and orphan or misreport proposals [K007][K008][K009], route-level drift between near-duplicate handlers [K085][K089], and a tool layer that fabricates `on_error="discard"` at five production sites, which ADR-040 §3 bullet 2 forbids because the runtime has no default for that field [K166].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K064] | Multi-query output_fields suffix equal to response_field (or _model/_usage) accepted and silently overwritten at runtime | Medium | TRACKED | S08 |
| [K081] | Deferred-blob classification misses markers in multi-query queries.<name>.template [R17] | Medium | PREVIOUSLY-REPORTED | S11 |
| [K109] | Multi-query llm prompt review with no node-level template cannot be approved [R06] | Medium | PREVIOUSLY-REPORTED | S22 |
| [K001] | Profile-only azure_ai_search node always fails composer Stage 1 (only llm has an inert probe stub) [R01] | Medium | REPORTED-AND-TRACKED | S08, S19 |
| [K002] | Dual validation authority: composer state.py hand-mirrors core/dag guarantee/extras/type walks (Stage-1 vs runtime), parity kept by manifest and comment, incl. the QUEUE/ROW_UNION fan-in rule with no shared primitive | Medium | REPORTED-AND-TRACKED | S02, S10, X2a |
| [K007] | _durable_completion_gates reads the moving head so a recovery persist erases the blocked advisor fact [R14] | Medium | PREVIOUSLY-REPORTED | S12, S15 |
| [K008] | Review-only save moves the head and orphans same-turn proposals [R03] | Medium | PREVIOUSLY-REPORTED | S12, S15 |
| [K009] | Decision-only (content-equal version bump) save invisible to frontend readiness refresh [R04] | Medium | PREVIOUSLY-REPORTED | S15 |
| [K060] | Plugin constructors double as validators; non-PluginConfigError raise can crash composer validation | Medium | TRACKED | S07 |
| [K066] | Source-hash lint skips plugins/transforms/aws though 4 AWS plugins declare source_file_hash | Medium | REPORTED-AND-TRACKED | S08 |
| [K067] | Integer output-field validator accepts integral floats and passes the float through | Medium | REPORTED-AND-TRACKED | S08 |
| [K085] | send_message and recompose ~82% duplicated and drifted: recompose lacks GuidedCustodyIntegrityError arm, weaker cancel join, no transcript Tier-1 guard (plus R40 copy) | Medium | REPORTED-AND-TRACKED | S15 |
| [K089] | /execute bare except ValueError maps envelope/policy refusals to 404; BlobRowsSourceAdmissionError and InlineBlobPromptSurfaceAdmissionError unmapped -> 500 | Medium | TRACKED | S16 |
| [K166] | Composer tool layer fabricates on_error="discard", which ADR-040 §3 bullet 2 forbids. The claimed construction-boundary inheritance gap is refuted. | Medium | PREVIOUSLY-REPORTED | R2: D18 |

#### M4. Guided-lane retirement blast radius (9)

Guided mode is retiring but its code is load-bearing for freeform: fork and revert run on the guided-operations ledger [K012], the tutorial runs on the guided lane [K013], and non-guided backend and frontend code import guided-named modules [K014][K015]. Retirement is therefore an extract-then-delete programme inside shared god-files, not a delete-by-name [K016]. Guided residue can already affect a freeform user. The guided-replay dedupe can hide a later freeform user message from the rendered transcript, although the server's `chat_messages` and the audit trail are unaffected. This needs three things: a terminal guided session, a declined revision or correction whose user turn has no `chat_messages` twin, and an exact re-send of the same text. Reach is therefore narrow [K162]. ADR-031's canary premise has also been stale since guided was retired as a user mode. Freeform, now the only authoring surface, has never had a non-adaptive fixed-script canary, and the ADR has not been amended. The freeform gap predates the retirement, which turned it into a gap on the primary surface [K160].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K012] | Freeform session fork and state revert run on the guided_operations ledger (reserve_or_replay_guided_operation, settle_guided_fork_operation, guided-branded error envelopes); deleting guided by name breaks fork/revert | Medium | NEW | S13, S14, S15 |
| [K013] | Tutorial runs on the guided lane (backend wizard + frontend tutorial<->chat<->chat/guided import cycle), so guided retirement is blocked until the tutorial migrates | Medium | REPORTED-AND-TRACKED | S13, S22 |
| [K014] | Backend non-guided code (freeform set_pipeline, composer state/planner/prompts) depends on guided-named modules and misfiled shared symbols (canonical_sink_local_paths, InvariantError, BLOB_REF_PATH_PREFIX, ...); deleting guided/ breaks freeform | Medium | NEW | S13, S11, S10 |
| [K015] | Frontend guided-named modules (chat/guided/*, guidedDecoder.ts incl. decodeCompositionState, types/guided) are shared infrastructure for non-guided surfaces; guided residue gates App/inspector/CommandPalette | Medium | NEW | S22, S23, S21 |
| [K016] | Guided behaviour embedded in shared god-files (>=41% of sessions/service.py, 31% of sessions/protocol.py, 590 lines composer/service.py; sessionStore 744 / ChatPanel 599 refs) | Medium | NEW | S13 |
| [K017] | Dead or near-dead guided surfaces: /guided/plan 0 production callers, unmounted ModeSwitchButton guided arm, /convert reachable only via unselectable preference | Medium | TRACKED | S13 |
| [K019] | post_guided_respond is a single 2,920-line async function; post_guided_chat_schema8 1,309 lines | Medium | NEW | S13 |
| [K160] | ADR-031's canary premise has been stale since guided was retired as a user mode. Freeform, now the only user authoring surface, has never had a non-adaptive fixed-script canary, and the ADR has not been amended. | Medium | PREVIOUSLY-REPORTED | R2: D12 |
| [K162] | Guided-replay dedupe matches on trimmed content and hides a later freeform user message when a guided user turn has no chat_messages twin | Medium | TRACKED | R2: D14 |

#### M5. Security and trust boundaries (9)

Input bounding and secret hygiene on surfaces that now face multiple authenticated users: unbounded expression evaluation [K037][K038], a secret scrubber with known misses [K035], a credential regex that rejects ordinary dotted identifiers [K101], unchecked frontend casts at the Tier-3 boundary [K033], and proxy-header handling that collapses every client into one rate-limit bucket [K004]. In the People & access panel, recovery for a half-deleted local account exists only in component state and is lost on reload, although a new code comment says it survives one [K182].

**Row content sent to an observability vendor (a Low, recorded here because it crosses a trust boundary; critic gap G3).** Web-authored pipelines cannot turn LLM tracing on: the provider-config policy rejects any non-null tracing for LLM transforms and LLM sources [K170]. On the YAML/CLI surface, an operator who sets `tracing.provider=langfuse` sends every call's system and user messages verbatim, which includes templated row data, plus the response content, to the configured Langfuse host. Langfuse has no switch to turn content recording off. The feature is opt-in and documented, including a privacy section, so the verifier classes it as a privacy posture and a gap in the architecture baseline, not as a defect [K170]. The `azure_ai` half of the gap as the critic stated it is refuted. Its content-recording default is `True`, but on a standard install the path fails at `on_start` with an ImportError, and its content switch governs an SDK that the Azure provider never calls [K170]. The verifier suggested that this broken `azure_ai` path get a separate row at about Medium. No such cluster exists in VC, so this document does not count it as a finding [K170].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K004] | uvicorn launched without proxy_headers/forwarded_allow_ips: behind nginx all clients share one IP, one auth rate-limit bucket, and over-limit auth_failure audit rows are dropped globally [R11] | Medium | REPORTED-AND-TRACKED | S09, S20, S18 |
| [K033] | Frontend Tier-3 boundary: parseResponse<T>/get<T> unchecked casts across client.ts and identity modules; strict CompositionState decoder bypassed on sendMessage/recompose/resolveInterpretation writes | Medium | REPORTED-AND-TRACKED | S21 |
| [K035] | scrub_text_for_audit lets AWS secret access keys and bare sk-ant- Anthropic keys through | Medium | NEW | S01 |
| [K037] | Expression parser raises raw RecursionError on deep inputs, bypassing typed-error handlers; no length/depth cap | Medium | TRACKED | S02 |
| [K038] | String-amplification guard only on composer preview path; runtime gate/trigger evaluation unguarded | Medium | TRACKED | S02 |
| [K068] | aws_textract_inline_analysis is USER_CONFIGURABLE with ambient default_chain credentials while async sibling is OPERATOR_PROFILED | Medium | NEW | S08 |
| [K101] | Credential regex (jwt pattern) rejects/redacts ordinary dotted identifiers like customer.address.city | Medium | NEW | S19 |
| [K107] | Cross-tab logout broken: no storage listener for auth_token [R10] | Medium | PREVIOUSLY-REPORTED | S21 |
| [K182] | People & access: Finish-removal recovery for a half-deleted local account exists only in component state, so it disappears on reload or re-select, and a new comment claims it survives a reload | Medium | PREVIOUSLY-REPORTED | R2: G9-R19 |

#### M6. Event loop, concurrency and availability (8)

Synchronous database and file work on the async event loop stalls every request in the process [K086][K090][K096]; a PostgreSQL lock-order cycle has no deadlock handler [K005]; worker-pool saturation surfaces as 500 instead of 503 [K097]; telemetry flush can hang forever [K070]; and one cancellation path is invisible to recovery [K006]. Each web process or replica also runs one pipeline at a time. Other users' runs wait in an unbounded FIFO queue with no per-user or global bound, no fairness and no run-duration cap. The design is deliberate, and the verifier classes it as a capacity limit plus baseline doc drift, not a correctness defect [K150].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K005] | PostgreSQL lock-order deadlock: WS ticket/progress writers (sessions->identities) vs provider-attempt/LLM-call admission (identities->sessions), no 40P01 handler [R05] | Medium | PREVIOUSLY-REPORTED | S16, S17 |
| [K006] | Run cancelled after permit (saga cancel_pending) with failed output finalisation is invisible to recovery; output blobs stay pending [R13] | Medium | PREVIOUSLY-REPORTED | S16, S17 |
| [K070] | TelemetryManager.flush() hangs forever once export thread dies with queued events (unbounded queue.join before re-raise) | Medium | NEW | S09 |
| [K086] | YAML export route runs synchronous lock-taking session transaction on the event loop | Medium | NEW | S15 |
| [K090] | Execution fan-out guard does blocking whole-file I/O on the event loop | Medium | NEW | S16 |
| [K096] | Synchronous Landscape auth-audit writes on the event loop in bearer gate and auth routes | Medium | NEW | S18 |
| [K097] | AsyncWorkerAdmissionTimeoutError (errno=None OSError) on auth path becomes 500 instead of 503 | Medium | NEW | S18 |
| [K150] | Web execution runs one pipeline at a time per web process/replica (ThreadPoolExecutor max_workers=1); other users' runs queue FIFO with no global or per-user bound, fairness, run-duration cap or queue signal, and the baseline does not state this | Medium | PREVIOUSLY-REPORTED | R2: D02 |

#### M7. Structure and coupling (9)

The layering and ownership problems behind §3: only L0–L2 import direction is gated [K024], session-table DML authority is split across two packages [K034], package cycles with private-symbol crossings [K142], a composer MCP server that hand-mirrors web internals [K071], duplicated Tier-3 parsers [K058], and a frontend with no import boundaries [K110].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K024] | Import-direction enforcement covers only contracts/core/engine (L0-L2); all other packages are L3 'may import anything', so cli/cli_plugins/composer_mcp->web inversions, plugins sub-package cycles, telemetry isolation and web-internal layering are ungated | Medium | NEW | S09, X2a, X1 |
| [K034] | Session-table DML authority split between sessions/service.py and coordination/repository.py with private-symbol imports both ways (web.coordination <-> web.sessions 40<->40 cycle) | Medium | TRACKED | S14, X1 |
| [K142] | web.execution <-> web.composer cycle (23 <-> 16) with execution taking private composer.state helpers | Medium | NEW | X1 |
| [K071] | Composer MCP server binds to web composer private internals and hand-mirrors the audit envelope | Medium | REPORTED-AND-TRACKED | S09 |
| [K087] | Sessions routes hold domain orchestration and import private symbols across modules | Medium | NEW | S15 |
| [K100] | blobs_table has two writer packages; quota admission split three ways | Medium | NEW | S19 |
| [K058] | Three separate Tier-3 CSV/JSON parsers (csv_source, azure_blob_source, aws_s3_source) with divergent hardening/options models | Medium | NEW | S07 |
| [K094] | Six Sessions-DB clock implementations with divergent aware-non-UTC handling; docstring says three | Medium | REPORTED-AND-TRACKED | S17 |
| [K110] | Frontend has no internal layering: 21-bucket runtime import SCC, no lint boundary rule | Medium | NEW | S23 |

#### M8. God objects and oversized units (8)

The measured complexity hotspots of §4 that verification confirmed as concerns [K055][K073][K074][K075][K079][K083][K108][K114].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K083] | SessionServiceImpl god object (10,546 lines, 172 methods, three transaction-ownership models) | Medium | REPORTED-AND-TRACKED | S14 |
| [K074] | composer service.py fuses six responsibilities; advisor ~2,878 lines still in service | Medium | REPORTED-AND-TRACKED | S10 |
| [K075] | Very large single functions in composer (_check_schema_contracts 2,779, run_tool_batch 2,028, ...) | Medium | TRACKED | S10 |
| [K055] | RowProcessor god-class residue (5,161 lines; extracted engines reach 33 private members) | Medium | TRACKED | S06 |
| [K079] | tools/_common.py 3,971-line sink mixing >=8 responsibilities; false L3 layering; fans out to 7 planes | Medium | TRACKED | S11 |
| [K073] | cli.py 4,802-line monolith with copy-shaped command bodies (join/resume/run/bootstrap_and_run drift risk) | Medium | TRACKED | S09 |
| [K108] | ChatPanel is a 3,094-line single function fusing 8+ responsibilities | Medium | NEW | S22 |
| [K114] | GraphView 943-line useMemo re-infers topology and runs dagre on every change incl. selection | Medium | NEW | S23 |

#### M9. Deployment and operator surfaces (8)

Deployment qualification does not match production: the ACA probe pins a topology production never uses [K105], PostgreSQL TLS is a deployment contract only on AWS [K106], a Linux-only rename primitive is unverified on the network filesystems both clouds use [K084], and peer compatibility columns are written but never read [K093]. Nothing at runtime checks the PostgreSQL server version, while the ACA bundle defaults to PostgreSQL 17 and every PostgreSQL proof runs on 16 [K154]. The product tutorial, which is the ADR-031 canary, fetches its scrape fixtures at runtime from public GitHub Pages. Egress-restricted deployments therefore cannot run it, and a change to that site alters tutorial behaviour without a release [K161].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K072] | elspeth explain / TUI (and other --database overrides) cannot open a PostgreSQL Landscape | Medium | NEW | S09 |
| [K084] | Archive quarantine requires Linux renameat2(RENAME_NOREPLACE); unverified on EFS/NFS data_dir | Medium | NEW | S14 |
| [K093] | No runtime compatibility check between peers; web_instances epoch/protocol columns written but never read | Medium | NEW | S17 |
| [K105] | Probed ACA topology (2/2 replicas) never equals production (2/4) | Medium | NEW | S20 |
| [K106] | PostgreSQL TLS checked only for aws-ecs, not a deployment-contract property | Medium | PREVIOUSLY-REPORTED | S20 |
| [K124] | Gateway conformance kit cannot qualify a real derived adapter (mock-specific assertions) | Medium | NEW | S25 |
| [K154] | No runtime check of the PostgreSQL server version; the ACA bundle defaults to PG 17 while every PG proof runs on 16 | Medium | NEW | R2: D06 |
| [K161] | The product tutorial (the ADR-031 canary) fetches its scrape fixtures at runtime from public GitHub Pages; egress-restricted deployments cannot run it, and a site change alters tutorial behaviour with no release | Medium | PREVIOUSLY-REPORTED | R2: D13 |

#### M10. Test and CI enforcement (10)

The gates that exist do not all run: static-analysis stops at its first red and skips 9 later gating steps, hiding three red steps of which one is real drift [K116], two rule sets and three jobs never report [K117][K118], the layer rule is not evaluated on push [K119], and whole test selections and linters run nowhere [K031][K112][K127].

| K-id | Title | Final severity | Provenance | Slice(s) |
|---|---|---|---|---|
| [K116] | Deliberately-red static-analysis job masks >=3 independent live regressions (FG3 chat_solver.py:374, old_outcome_string_compare, fingerprint mismatch) | Medium | NEW | S24 |
| [K117] | trust_boundary.{tests,scope,tier} and parity harness have no if: always() twin so never run in CI while earlier steps are red | Medium | NEW | S24 |
| [K118] | state-engine-validation, azure-container-apps-bicep and supply-chain-audit still needs: static-analysis, so they are skipped | Medium | TRACKED | S24 |
| [K119] | ADR-006 layer enforcement not evaluated in push CI (tier-model step exits 2 at allowlist load) | Medium | NEW | S24 |
| [K030] | Browser E2E never drives the composer LLM loop (all provider keys blanked; state seeded; fixtures); 5 specs unconditionally skipped ~80 days; tests/e2e/README claims continue-on-error that ci.yaml lacks | Medium | TRACKED | S23, X3 |
| [K031] | Selections no CI job runs: performance-marked NFR benchmarks (ADR-008/010 'CI benchmark is the enforcement mechanism'), slow, Hypothesis nightly profile | Medium | TRACKED | X2a, X3 |
| [K112] | ESLint/stylelint and typecheck:workspace-e2e run in no CI workflow or pre-commit | Medium | TRACKED | S23 |
| [K113] | E2E specs assert a Fullscreen control GraphView renders only when nodes exist, on an empty session [R20] | Medium | PREVIOUSLY-REPORTED | S23 |
| [K127] | No CI lint or type gate for gateway/ or evals/ | Medium | NEW | S25 |
| [K147] | Testcontainer CI sizing stale (sized on 276 ids; 519 collected plus 5 outside files) | Medium | NEW | X3 |


### 2.3 Low findings, summarised by theme

The 79 Low clusters were verified like the Mediums [VC §Outcome]. 56 of them are downgraded Mediums or cross-cut gaps from the first pass. The other 23 come from the R2 gap round (K149–K179, excluding the refuted and Medium ids), which verified behavioural baseline-delta rows and web-review ids that the catalog had filed only as Low rows, sometimes in two or three slices at once [VC §Method][VC §Outcome]. The remaining catalog Low rows (241 in the concern tables) were *not* re-verified and are not counted here [VC §Method].

| Theme | Count | K-ids | What they are |
|---|---:|---|---|
| Guided and tutorial residue | 6 | [K018] [K020] [K021] [K022] [K139] [K159] | Surfaces that must be removed or re-homed when guided retires, the tutorial's frontend script branches awaiting a ruling, and a readiness row that detects the tutorial by pipeline shape |
| Structure and layering | 9 | [K025] [K036] [K042] [K076] [K095] [K141] [K143] [K144] [K145] | Package cycles, misplaced vocabulary and private cross-module access (see §3) |
| ADR and documentation drift | 18 | [K027] [K028] [K029] [K057] [K082] [K129] [K130] [K131] [K133] [K134] [K135] [K152] [K156] [K165] [K167] [K171] [K176] [K179] | ADR clauses superseded or widened without amendment, stale status fields and stale baselines (see §6), plus operator and planner-facing text that the code contradicts |
| Engine, Landscape, audit and data-path correctness | 14 | [K039] [K041] [K046] [K050] [K054] [K069] [K091] [K092] [K137] [K138] [K140] [K153] [K172] [K175] | Latent or narrow-reach correctness gaps, unwired or vestigial ledger code, Tier-1 reads with defaults, and two audit rows that record the wrong actor or the wrong failure class |
| Composer, web and frontend robustness | 12 | [K023] [K077] [K078] [K080] [K099] [K111] [K149] [K151] [K169] [K173] [K174] [K178] | Composer seams and frontend failure handling; K023 is the operator-sanctioned required-control carve-out, recorded only in code |
| Security hygiene | 7 | [K098] [K115] [K125] [K136] [K148] [K158] [K170] | Narrow-reach boundary gaps: SQLite auth store on NFS/EFS, client-derived egress disclosure, shape-only gateway identity, one ADR-032 Protocol gate, a live test outside its fence, an unauthenticated status endpoint, and opt-in CLI tracing that sends row content to Langfuse |
| Enforcement and test infrastructure | 7 | [K032] [K120] [K121] [K122] [K128] [K146] [K164] | Gate and suite plumbing: timeouts, root-sensitive silent inertness, allowlist governance debt, build-push reachability, walker-regex gap, an evals harness half-tracked, and a wheel-shipped pytest plugin |
| Deployment, operator surfaces and satellites | 6 | [K059] [K104] [K126] [K157] [K168] [K177] | Operational defaults, satellite packaging, telemetry filtering and metrics planes, and config validated only at run time |

Titles, for scanning:

- **Guided and tutorial residue:** [K018] Server/DB still accept default_composer_mode='guided'; the UI disable is cosmetic; [K020] Every freeform session open/fork probes GET /guided?probe=true and treats 400 as freeform-only; [K021] ACA replica fence probe P1 drives /guided/respond and counts guided_operations rows; loses its endpoint/table when guided is removed; [K022] evals repros and website release test import guided modules/GUIDED_SESSION_SCHEMA_VERSION (removal inventory); [K139] ADR-031 tutorial privilege implemented as 39 isTutorial conditionals across 12 frontend files; passthrough auto-proposal suppressed only in tutorial; [K159] audit_readiness picks out the tutorial by composition shape and adds a relabelled "Tutorial required controls" row to any pipeline of that shape.
- **Structure and layering:** [K025] Intra-core import SCC (core.checkpoint <-> core.landscape) carried by checkpoint.serialization, NonResumableRunError and eager core/__init__ + landscape/__init__ facades; hidden 19-module import-order-dependent cycle; plus whole-tree SCC counts; [K036] Web-domain vocabulary (guided fences, session fork, quotas, interpretation) drifting into L0 contracts with no ownership record; [K042] ExecutionRepository is not the pure facade its docstring claims (complete_aggregation_result 356 lines of SQL); facade migration stalled; [K076] Cross-module private access in composer core (tool_batch writes service privates; boot_probe imports private LLM helpers; provider transport not its own module); [K095] interpretation_state.py is composer logic at web root, cycled with composer.state through private symbols; [K141] web.sessions <-> web.composer package cycle (19 <-> 133 statements; 60% guided) and minimal seam; [K143] web.auth <-> web.(root) cycle (20 <-> 21) via config.py importing auth.providers/urls; [K144] Role-based web layering: 33 upward edges (10 acceptance misfiles, 21 domain->service incl. composer.service private LLM helpers); [K145] 41 mechanical lazy-import cycle-breakers (20 pairs); removing them reduces runtime SCCs to [17,2].
- **ADR and documentation drift:** [K027] Stale audit-store architecture baseline: ARCHITECTURE.md / landscape.md epoch and table counts, component list, self-contradictory landscape.md; ADR-048 still 'Proposed'; [K028] ADR-048 status says Proposed for a ~98%-implemented, gate-green decision; _FRESH_EPOCH_ONE_EXCEPTION 'non-release' sunset text present on release/0.8.1 (Task 8B blocker vs stale sunset needs a maintainer ruling); [K029] ADR-037 6th enum member SOURCE_DATA_CONTRACT added without mandatory assessment/amendment; superseded ADR-026/029/035/037/038 prose; [K057] BaseSink.write/flush remain abstract and documented while every built-in sink raises; real effect contract is a marker class; [K082] Advisor END gate (completion-blocking control) has no ADR; [K129] ADR-001 single-threaded claim now per-worker; IdleTimeoutPump writes audit records from its own thread under a CV handshake; [K130] ADR-019 run-status predicate drifted: quarantine counted clean, rows_coalesce_failed an off-table failure input; [K131] ADR-004 terminal-only on_success and ADR-003 NullSource resume superseded without banners; redundant _build_resume_graphs; [K133] ADR-010 A4 says every registered violation_class is tier_1 but UnexpectedEmptyEmissionViolation is Tier 2 per ADR-012; [K134] ADR-021 transform boundary set widened (IO_READ, phase-7a-v3) without ADR update; [K135] ADR-023 SARIF not uploaded to Code Scanning; category table stale; [K152] Web tier does not refuse a second concurrently active replica on targets documented as single-replica (AWS ECS, Kubernetes BYO, Compose, systemd on PG); [K156] Docs call telemetry BLOCK backpressure "complete"/"ensures all events delivered", but BLOCK drops an event after a 30 s put timeout and loses events on exporter failure; [K165] Tier-1 registry owner allowlist widens to 'tests.' when pytest is already imported; ADR-010 D2 does not mention it; [K167] The llm transform's get_agent_assistance summary names 3 providers, but _PROVIDERS has 4 (gateway is missing); [K171] ADR-026 D9 says the scrubbed scheduler payload keeps the payload's identity through its hash, but payload_hash is sha256 of an anchor id (barrier_key, token_id or work_item_id); [K176] azure_ai_search contract comment says the private-binding set is "shared by the plugin" but no plugin code reads it; [K179] The environment-variable reference is stale for the Composer timeout (180.0 vs 300.0), has no transport ceiling or headroom rows, and does not document EXECUTION_RATE_LIMIT.
- **Engine, Landscape, audit and data-path correctness:** [K039] build_execution_graph never calls graph.validate(); callers must remember; validate() skips route-label check with empty sink map; [K041] batch_outputs table created/validated but never written or read; unscoped FK; [K046] SourceCompletionReconciler repairs an image no current path produces; unreachable compatibility code scanned every resume; [K050] Follower wiring (PipelineConfig, PluginContext, on_start) hand-assembled in cli.py outside RunContextFactory; missing coordination_token and llm_call_governance; [K054] restore_from_journal (914 lines) runs durable journal releases inside its derivation phase; coalesce vs row_union reset-count mismatch handling asymmetric; [K069] Cross-provider LLM audit shape differs (OpenRouter/Gateway write HTTP + LLM rows; Azure/Bedrock one LLM row); [K091] Unbounded run_events persistence; PG poller runs count/min/max over all run events every 0.25s per client; [K092] Coordination protocol-v1 areas cleanup_claim and run_ownership_fence declared but unimplemented; [K137] PostgreSQL follower join not refused despite ADR-030/041 unsupported topology; [K138] Two web read paths open writable Landscape engines (tutorial_service, recovery.observe_run) incl. create_tables; [K140] Tier-1 audit reads with defaults/coercion (mcp queries.py context_after_json .get defaults; export_read_model tz; interpretation kind default); [K153] Landscape has no explicit dialect refusal; unsupported URLs fail at open with an incidental compile error, not late at the clock; [K172] Server-route interpretation rows (state_revert / yaml_import / e2e_seed) record actor='composer-llm'; on opted-out sessions that actor is sealed into arguments_hash of an immutable born-resolved row; [K175] Malformed/missing provider usage on the calculated-cost path is raised and audited as COST_UNAVAILABLE (pricing-config copy, SUCCESS audit row) instead of MALFORMED_RESPONSE.
- **Composer, web and frontend robustness:** [K023] required_controls inserts server-authored transform nodes into planner candidates at five seams; sanctioned by operator decision elspeth-f99655f540 but not ratified in an ADR or the AGENTS.md invariant-1 carve-out (which says admission gates); [K077] run_signoff_checkpoint Protocol/implementation signature drift (session_operation_context); 0 production callers; [K078] Withheld-reply rows written in separate transactions from the compose turn [R22]; [K080] Per-node admission gate chain hand-copied and drifted: set_pipeline never calls _validate_aggregation_trigger; different option dicts passed; [K099] Vestigial WEB_SURFACE_PROHIBITED: no producer, still dispatched across discovery code and wire contracts; [K111] No top-level React ErrorBoundary; App-level render error white-screens; [K149] Share-link resolve re-projects frozen blobs through the current build; pre-upgrade blobs fail strict nested validation and return 500, not 401 (R52); [K151] Composer per-user rate limit is checked after the in-flight dependency has already admitted a request lease; [K169] Frontend generate-types script targets :8000 while the backend defaults to 8451; its output was never produced and nothing checks the wire types; [K173] Interpretation-events refresh fence drops a successful older snapshot when the newer refresh fails, so a pending review card can stay invisible; [K174] cost_unavailable (503 "ask an administrator") still offers Retry; each retry is another provider call that cannot be priced; [K178] Proposal 409 conflict detail is dropped when a newer proposal-list snapshot supersedes the conflict refresh.
- **Security hygiene:** [K098] Local-auth auth.db is SQLite under data_dir even in external-postgresql mode (SQLite on NFS/EFS with replicas>1); [K115] Run-consent egress disclosure is client-derived and fail-open for unknown plugins; [K125] Gateway adapter identity at ELSPETH preflight is shape-only; stored value never read; [K136] ADR-032 drift: runtime_checkable Protocol isinstance gate on botocore S3 Body (aws_s3_source.py:459/461); [K148] Live Azure Key Vault test outside live-provider fence (gated only by TEST_KEYVAULT_URL, .env loaded); [K158] GET /api/system/status is unauthenticated and lets anyone read composer/advisor model names, provider, missing credential env-var names, plugin-readiness rows and deployment identity; [K170] LLM Tier-2 tracing sends row-derived prompt and response content to an observability vendor (Langfuse confirmed; the azure_ai half cannot be reached and its content switch is wired to an SDK the call path does not use). See the note under M5.
- **Enforcement and test infrastructure:** [K032] No global pytest-timeout; process-killing e2e/recovery files carry no mark.timeout; no rerun plugin; e2e mocks contradict conftest; elspeth-xdist-auto plugin inert under addopts -n 12; [K120] build-push can no longer produce images from main automatically since 64de4499d (push CI cannot succeed); [K121] Root-sensitive silent inertness: meta.no-new-bespoke-cicd-enforcer and test_to_source_mapping vacuous under --root src/elspeth; [K122] Tier-model governance debt: 10 expired signed entries, 201 stale, max_hits exceeded, per_file_rules at ceiling 37/37; [K128] evals ships tracked half of untracked composer-rgr harness; open tracker rows target files not in tree; [K146] Tree-walker authority regex misses rglob('*') + suffix filter; 3 tests walk production source unguarded; [K164] Wheel-shipped pytest11 plugin elspeth-xdist-auto forces `-n auto` on third-party pytest runs in any venv where elspeth and pytest-xdist are both installed.
- **Deployment, operator surfaces and satellites:** [K059] Remote-effect spool root defaults to CWD-relative path; resume from another CWD fails closed; no deploy sets ELSPETH_EFFECT_SPOOL_DIR; [K104] Production ECS boot depends on private _aws_ecs_acceptance.ecs_metadata module; hardcodes aws partition; [K126] Base gateway image not reproducible at Python-dependency level (no lock file), contradicting README; [K157] Telemetry granularity filter lets unknown event types through at every level, including `lifecycle`, and no test makes new event classes get classified; [K168] ACA ships with Prometheus pull metrics only; the OTLP push exporter and the pipeline-metric overlay exist only in AWS (aws-otlp) mode; [K177] WebSettings.execution_rate_limit.persistence_path is accepted at boot; its path rules run only per web run, so a bad path fails every run.

**Refuted.** Five clusters were refuted and are not findings [VC §Outcome]: K010 (R02), K045, K163 (R2 sweep item D15 — not 06's decision D15, an undeclared zero-emission success reaching `FILTER_DROPPED`, which the registered `CanDropRowsContract` blocks) [K163], K180 (web-review R15, fixed by `4afd73169` before the pin) [K180] and K181 (web-review R16, fixed by `1809379f6` before the pin) [K181]. K010, K180 and K181 matter for reconciliation (§7).

---

## 3. Structural quality

**The import-time module graph is acyclic.** Over 875 modules and 5,460 distinct module-level edges there are 0 strongly connected components. Two independent Tarjan implementations agree, and adding one reverse edge produces a 25-module SCC, so the instrument can detect cycles [X1 §0][X1 §2.4]. The longest import chain is 44 modules and the web subtree is 33 levels deep [X1 §0].

**The core layers hold, and `contracts` is a true leaf.** `contracts` has 0 outbound edges of any kind; core imports nothing from engine, plugins or web; engine imports nothing from plugins, web or UI; telemetry imports only contracts [X1 §1.2]. The two retained ADR-006 residuals (`core/config.py` and `core/llm_profiles.py` reaching into plugins) are gone from core; the loader moved to the top-level `config_loading.py`, which the lint classes as L3 by directory [X1 §1.2].

**Two baseline documents disagree on plugins → engine.** ADR-006 and the lint put plugins (L3) above engine (L2), so plugins → engine is legal; ARCHITECTURE.md draws engine and plugins as peers with no edge [X1 §1.1]. Three such edges exist (`runtime_factory.py` → `engine.orchestrator.preflight`, module and lazy; `_diversion_attribution.py` → the private `engine._error_hash`), and the question is unadjudicated [X1 §1.2][X1 §5.2]. One of them reaches a private module [X2a §2].

**The web "SCC" is a package-bucketing effect of vertical slices.** Web packages each span most levels of the module DAG (`web.sessions` 0–31, `web.composer` 0–24, `web.(root)` 0–32), so cross-slice imports at different heights produce a package cycle although every module import points down [X1 §0][X1 §2.6]. Deleting the guided lane, extracting `composer.state`, extracting the session store and moving every low-level module into a foundation package each still leave a 15–18 bucket package SCC [X1 §2.6]. The heaviest pairs are verified concerns: `web.coordination ↔ web.sessions` 40 ⇄ 40 with private symbols crossing both ways [K034], `web.execution ↔ web.composer` 23 ⇄ 16 with execution taking private `composer.state` helpers [K142], `web.sessions ↔ web.composer` 19 ⇄ 133 with 60 % of it guided [K141], and `web.auth ↔ web.(root)` 20 ⇄ 21 [K143].

**Role layering is already 98.3 % true.** Classifying web modules by filename as domain < service < routes < app leaves 1,898 of 1,931 web-internal import-time edges pointing down; 33 point up [X1 §2.7][K144]. Verification corrected X1's breakdown: 11 come from two misfiled Azure acceptance modules, 2 from the `shareable_reviews` package `__init__`, and 20 from domain → service, including three consumers of `composer.service`'s private LLM helpers [K144]. There are zero service → routes, service → app or routes → app edges [X1 §2.7]. The seam X1 recommends is horizontal role layers across the verticals, not vertical package acyclicity [X1 §0].

**41 lazy imports are mechanical cycle-breakers.** Of 701 internal lazy imports, 41 (20 distinct pairs) would create an import-time cycle if promoted; removing those 20 pairs reduces the runtime SCCs from `[52, 35, 12, 2×6]` to `[17, 2]` [X1 §3.3][K145]. Only 2 of the 41 carry a cycle comment, so ADR-006's "apologetic comment" signal has disappeared while the pattern persists, concentrated in `web.composer` (18, 13 of them in `state.py`) and `core.landscape` (7) [X1 §3.1][X1 §3.5]. The other 660 lazy imports break no cycle; `cli.py`'s 125 are a legitimate startup-latency pattern, while the deferred guided imports in `sessions/service.py` duplicate what `sessions.protocol` already loads at module level [X1 §3.5].

**Two web modules act as contracts without being contracts.** `web.composer.state` (9,064 lines, fan-in 71) and `web.sessions.protocol` (5,051 lines, fan-in 56) have contract-level fan-in but live in feature packages and carry heavy implementation [X1 §2.2]. retired code index's symbol-level coupling data independently ranks `CompositionState` (fan-in 244) as the web hub [X1 §4].

**A latent import-order hazard sits in core.** A 19-module cycle through `core.checkpoint` and `core.landscape` appears only when the implicit execution of package `__init__` facades is modelled; every member imports cleanly first today, so the cycle is tolerated and order-dependent [X1 §2.4][K025].

**What is and is not enforced.**

| Boundary | State at pin | Enforced by |
|---|---|---|
| contracts (L0) < core (L1) < engine (L2) < everything else (L3) | Holds, 0 L1/TC findings; mutation controls fire | `trust_tier.tier_model` rule L1, incl. lazy imports [X1 §5.1] — but not evaluated on push, and its PR signal is diluted in a standing red corpus [K119] |
| contracts is a runtime leaf | Holds | also `tests/unit/contracts/test_leaf_boundary.py` [X1 §5.1] |
| Two acceptance packages' internal layers | Holds | two architecture tests, the only web-internal layering gates [X1 §5.1] |
| Module-level acyclicity (whole tree) | Holds | **nothing** [X1 §5.2] |
| Web-internal layering, UI → web, plugins sub-package direction, telemetry isolation, plugins → web | Mostly holds or stable | **nothing**: all L3, and L3 files are skipped, covering about 71 % of production lines [K024][X1 §5.2] |
| Lazy-import growth | 701 lazy, 41 breakers | **nothing** [X1 §5.2] |
| Frontend import boundaries | 21-bucket directory-level runtime SCC | **nothing** [K110] |

The lint's remediation text points readers to a "CLAUDE.md Layer Dependency Rules" section that no longer exists, so ADR-006's "Violation #11 protocol" survives only in the ADR [X1 §5.1][X2a §2]. With more developers incoming, the unenforced 71 % is the structural risk: every boundary holds today, and nothing would turn red if one stopped holding [K024].

---

## 4. Complexity hotspots

### 4.1 Largest files, and growth against ARCHITECTURE.md

| File | Lines at pin | ARCHITECTURE.md figure | Source |
|---|---:|---|---|
| `web/sessions/service.py` | 15,006 | ~14,321 (flagged "Medium" priority) | [measured: wc -l at 85ebf2739][measured: grep -n ARCHITECTURE.md → lines 1096, 1110] |
| `web/composer/service.py` | 11,377 | ~10,298 | [measured: wc -l at 85ebf2739][K074] |
| `web/composer/state.py` | 9,064 | not listed | [measured: wc -l at 85ebf2739][X1 §2.2] |
| `web/sessions/routes/composer/guided.py` | 5,889 | not listed | [measured: wc -l at 85ebf2739][K019] |
| `web/coordination/repository.py` | 5,900 | not listed | [measured: wc -l at 85ebf2739][S13 §Complexity] |
| `engine/processor.py` | 5,549 | ~5,546 | [measured: wc -l at 85ebf2739][measured: grep -n ARCHITECTURE.md → line 235] |
| `web/sessions/protocol.py` | 5,051 | not listed | [measured: wc -l at 85ebf2739][X1 §2.2] |
| `web/execution/service.py` | 4,891 | not listed | [measured: wc -l at 85ebf2739][X1 §2.3] |
| `web/frontend/src/stores/sessionStore.ts` | 4,809 | not counted (frontend excluded) | [measured: wc -l at 85ebf2739][S21 §Complexity] |
| `cli.py` | 4,802 | not listed | [measured: wc -l at 85ebf2739][K073] |
| `web/composer/redaction.py` | 4,802 | not listed | [measured: wc -l at 85ebf2739][S12 §Complexity] |
| `web/composer/tools/_common.py` | 3,971 | not listed | [measured: wc -l at 85ebf2739][K079] |
| `web/frontend/src/components/chat/ChatPanel.tsx` | 3,897 | not counted | [measured: wc -l at 85ebf2739][K016] |

Production Python grew from ARCHITECTURE.md's ~455K lines / 805 files (measured 09-08) to 489,459 / 875 at the pin, +34K lines in 15 days, and `web/` is 53 % of it [01 §2]. The two service files ARCHITECTURE.md already names as a Medium-priority improvement grew by 685 and 1,079 lines respectively [01 §8][K074]. ARCHITECTURE.md does not count the frontend at all. The frontend is 69,903 lines of production TS/TSX in 259 files, plus 132,755 lines of test code in 338 files [00 §Measured size][01 §2]. The 202,658 total includes those test files, so it must not be set beside the production-only Python figure [measured: `git ls-files` for `*.ts`/`*.tsx` under `src/elspeth/web/frontend` at 85ebf2739, test = `.test.`/`.spec.` suffix or a `test/`, `tests/`, `__tests__/` or `e2e/` directory → 259 / 69,903 production, 338 / 132,755 test, total 202,658. This agrees with the critic's independent two-classifier split, and `stores/sessionStore.ts` is present in the list as a positive control].

### 4.2 Largest functions and classes

| Unit | Size | Source |
|---|---:|---|
| `SessionServiceImpl` (class) | 10,546 lines, 172 methods, three transaction-ownership models | [K083] |
| `RowProcessor` (class) | 5,161 lines; `__init__` 540 lines, 39 parameters | [K055] |
| `ExecutionServiceImpl` (class) / `_run_pipeline` | 3,805 / 1,483 lines; `_run_pipeline` settles on 7 local flags | [S16 §Complexity] |
| `ChatPanel` (React function) | 3,094 lines, ~138 hook calls per render | [K108] |
| `post_guided_respond` (async route) | 2,920 lines; `post_guided_chat_schema8` 1,309 | [K019] |
| `_check_schema_contracts` / `run_tool_batch` / `_validate_with_probe_cache` / `_plan_pipeline_inner` | 2,779 / 2,028 / 1,645 / 1,568 lines | [K075] |
| `RowTokenRepository` (class) | 2,040 lines, writes 9 tables | [S04 §Complexity] |
| `BlobServiceImpl` (class) | 2,013 lines | [S19 §Complexity] |
| `RunLifecycleRepository` (class) | 1,945 lines | [S03 §Complexity] |
| `RepositoryIdentityAuthority` (class) | 1,942 lines | [S17 §Complexity] |
| `build_execution_graph` (function) | 1,829 lines, ~20 sequential phases | [S02 §Complexity] |
| `TierModelVisitor` (lint class) | 1,718 lines | [S24 §Complexity] |
| `ExecutionGraph` (class) | 1,554 lines | [S02 §Complexity] |
| `GraphView` (React function) | 1,432 lines; one 943-line `useMemo` | [K114] |
| `build_set_pipeline_candidate` (composer tool) | 1,255 lines | [S11 §Complexity] |
| `WebSettings` (class) | 1,222 lines, 114 fields | [S18 §Complexity] |
| `send_message` / `recompose` routes | 1,063 / 740 lines, ~82 % duplicated | [S15 §Complexity][K085] |
| `restore_from_journal` | 914 lines | [S06 §Complexity][K054] |

Sizes tagged with a K-id were re-measured by verification; the others come from catalog Complexity sections and carry the catalog's residual error rate (§9) [VC §Method][00 §Execution Log].

### 4.3 God-object decompositions the slices identified

- **`web/sessions/service.py`.** `SessionServiceImpl` runs on three transaction-ownership models (operation-authority `mutate`, process-locked begin, guided-session mutation transactions) [K083]. At least 41 % of the file is guided-named code across 83 definitions [K016]. Session-table DML authority is split between this file and `coordination/repository.py` with private-symbol imports both ways [K034]. The slice's decomposition order is: re-home fork before any guided removal, because freeform fork and revert run on the guided-operations ledger [K012][S14 §Complexity].
- **`web/composer/service.py`.** It fuses six responsibilities; about 2.9–3.0K lines are advisor code although five `advisor_*` modules exist and hold only 871 lines [K074]. An LLM-call primitive (`_litellm_acompletion` and siblings) lives inside the 11K-line service and is imported privately by three other modules [X1 §2.7][K144].
- **`web/composer/state.py`.** Its 9K-line domain model calls helpers that import it back (a 12-module runtime SCC), a split-module smell rather than a layering one [X1 §3.5]; it hand-mirrors the core/dag guarantee walks [K002].
- **`engine/processor.py` (`RowProcessor`).** `TokenTraversalEngine` reaches 33 distinct private processor members through 76 references, and tests patch the private `_process_single_token` at 33 sites [K055]. The file still fuses ingest, resume dispatch, flush routing, member-loss settlement, four barrier fire completions and committed-residual recovery [S06 §Complexity].
- **`web/composer/tools/_common.py`.** 101 top-level definitions mixing unrelated responsibilities; all 7 tool planes and 14 production modules outside `tools/` import it, and its docstring's stated import surface is false [K079].
- **`cli.py`.** Copy-shaped command bodies: 9 settings loads, 8 `SecretLoadError` handlers, 5 twelve-keyword graph builds, 10 `LandscapeDB.from_url` calls [K073]; follower wiring is hand-assembled here outside `RunContextFactory` [K050].
- **Frontend.** `sessionStore.ts` fuses session list, transcript, composition state, proposals, polling, recovery, fork, history and the whole guided protocol [S21 §Complexity]; `ChatPanel` fuses 8+ responsibilities [K108]; `GraphView` re-infers topology and runs dagre on every selection change [K114].

**Debt markers.** There are 0 TODO/FIXME markers in `src/elspeth` Python; deferred work is written as prose with ticket ids, so marker counts are not a debt measure here [S05 §Complexity][S17 §Complexity].

---

## 5. Test architecture quality

**Shape: steep and bottom-heavy, not inverted.** `tests/` holds 2,380 `.py` files, 1,272,191 lines and 38,938 AST test functions (parametrisation unexpanded); unit is 80.1 % of test lines and e2e 1.8 % of tier lines [X3 §1.1]. The frontend has 269 Vitest files (116,502 lines) against 264 source files (70,062 lines), a 1.66 ratio [X3 §5]. X3's classifier differs slightly from the corrected production/test split, which gives 69,903 production lines in 259 files. The ratio is about the same either way [00 §Measured size].

**Architecture is enforced by closed-world inventories in tests.** 38 test files walk the tree through the sanctioned walker [X3 §3.2]. Authority boundaries (Sessions-DB writers, Landscape DML, database clocks, composer wire keys) are pinned manifests; the two largest pin files hold 38,984 lines of reviewed manifest data, so every change touching a pinned site needs a re-derive in the same commit and a scoped local run cannot certify it [X3 §3.2]. `test_mock_discipline_baseline.py` makes unspecced mocks a whole-repository failure [X3 §8.2]. No measured module is covered only by mocks [X3 §8.2]. The testcontainer PostgreSQL suite has one URL seam, itself pinned by an architecture test [X3 §4].

**Gaps (verified):**

- Browser E2E never drives the composer LLM loop: every provider key is blanked, composer interactions are fixtures or seeded state, and 5 of 22 non-staging specs have been unconditionally skipped for about 83 days [K030]. One spec asserts a control that is never rendered on an empty session, so it fails in the required E2E job [K113].
- No CI job selects `performance`, `slow` or `stress` tests or the Hypothesis `nightly` profile, so the ADR-008/ADR-010 benchmarks that ADR-010 calls its enforcement mechanism never run [K031].
- The required Testcontainer job's 30-minute timeout was sized on 276 ids; it now selects about 560 and took 26m08s on a hosted runner on 2026-09-22 [K147].
- No global pytest timeout; the process-killing `e2e/recovery` files carry no `mark.timeout`; there is no rerun plugin [K032][X3 §7].
- The walker-authority regex misses `rglob("*")` plus a suffix filter, so 3 tests walk production source outside the frozen-tree guard [K146].
- A live Azure Key Vault test sits outside the live-provider fence, gated only by an env var that `.env` can supply [K148].
- ESLint, stylelint and the workspace-e2e typecheck run in no CI job or hook [K112]; gateway/ and evals/ get no CI ruff or mypy, and strict mypy already finds 56 errors in gateway/src [K127].
- Coverage floors exist for landscape (92), `canonical.py` (99), orchestrator (90) and contracts (62), but not for `web/`, which is 53 % of production Python; mutation testing covers only `canonical.py` and `landscape/` and never gates [X3 §8.1][X3 §6].
- X3's unit-only list (no integration, e2e or testcontainer importer) names `composer_mcp`, `mcp`, both acceptance packages and `web.shareable_reviews`, although its own annotations give `mcp` and `web.shareable_reviews` one integration file each [X3 §8.1]; `web.coordination`'s coverage lives mostly in the PostgreSQL-only job, which a default `pytest tests/` never runs [X3 §8.1].

**CI selections that never run** [X3 §6][K031]: `tests/performance/` (26 files, 77 test functions), 9 `slow`-marked files, the Hypothesis `nightly` profile, and `live_provider` nodes (by design, manual dispatch only). The `integration` job is not in `ci-success.needs` [X3 §6].

---

## 6. ADR conformance scorecard

**Counting rule.** One verdict per ADR, taking the worst verdict of any of its clauses. ADR-010 is listed under both PARTIAL and DRIFTED in X2a; under this rule it counts once, as DRIFTED (its NFR clause) [X2a §1]. ADR-048 is conformant in code and is counted as conformant [X2b §Part A]. X2b flagged its "Proposed" status as drift, but verification did not uphold that. "Proposed" is deliberate until the ADR's completion step, Task 8B (removing the epoch-one exception), lands, and Task 8B has not landed. What remains is a Low release-notes gap, listed separately below [K028]. K027's verified claim still lists ADR-048's "Proposed" among its stale-baseline items. This document follows K028, the cluster about ADR-048 itself [K027][K028]. ADR-044 and ADR-045 do not exist [X2b §Part A].

**R2 clusters and this scorecard.** The verdicts below are X2a's and X2b's, and the R2 clusters postdate them. Where an R2 cluster bears on an ADR that already has a row, it is added to that row: K154 (041), K160 (031), K165 (010) and K171 (026) [K154][K160][K165][K171]. K155 does not change a verdict: it conforms to ADR-034's wording, which asks only for "a dedicated audit table", and its gap is against Landscape audit primacy, not against the ADR [K155]. K166 is different. X2b rated ADR-040 CONFORMANT but marked "No fabricated defaults in the YAML generator" as not exhaustively verified (INFERRED), and it did not audit every Stage-1 rule [X2b §Part A]. K166's fabrication sites are in the composer tool layer and the guided planner, not the YAML generator, so they sit outside what X2b checked [K166]. K166 finds the composer tool layer fabricating `on_error="discard"`, which ADR-040 §3 bullet 2 forbids [K166]. Under the counting rule, ADR-040 therefore scores at least PARTIAL. The table keeps X2's totals so that it stays traceable to X2; with K166 applied they become 28 CONFORMANT and 12 PARTIAL, and ADR-040 gets its own row below.

| Range | CONFORMANT | PARTIAL | DRIFTED | SUPERSEDED | RETIRED | UNVERIFIABLE | Total |
|---|---:|---:|---:|---:|---:|---:|---:|
| ADR-001–024 [X2a §1] | 12 | 8 | 2 (010, 019) | 1 (018) | 1 (024) | 0 | 24 |
| ADR-025–049 [X2b §Part A] | 17 (15 + 043 in-tree + 048 code) | 3 | 1 (032) | 0 | 0 | 2 (046, 049) | 23 |
| **All present ADRs** [X2a §1][X2b §Part A] | **29** | **11** | **3** | **1** | **1** | **2** | **47** |

Six of the 15 conformant ADRs in the second range carry superseded prose or an unrecorded assessment (026, 029, 035, 037, 038 and the anchors in 025) [X2b §Part A][K029].

**Every DRIFTED and PARTIAL item, plus the prose drift on conformant ADR-026, the release-notes gap on ADR-048 and the R2 finding against ADR-040, with its cluster** [X2a §2][X2b §Part A]. Rows and text marked (R2) come from the R2 clusters:

| ADR | Verdict | What drifted | Cluster |
|---|---|---|---|
| 019 | DRIFTED | The normative run-status predicate: quarantine counts as clean and `rows_coalesce_failed` is an off-table failure input, with no ADR amendment | [K130] |
| 010 | DRIFTED (NFR clause); PARTIAL (audit-complete, A4) | CI benchmark "enforcement mechanism" never runs; no declaration-contract dispatch at the collector closer; A4 says every violation class is Tier 1, but ADR-012 makes one Tier 2. (R2) D2 does not mention that the Tier-1 registry owner allowlist widens to `tests.` when pytest is already imported | [K031][K132][K133][K165] |
| 032 | DRIFTED (1 site, 2 calls) | `runtime_checkable` Protocol `isinstance` used as an admission gate on the botocore S3 body | [K136] |
| 001 | PARTIAL | "Orchestrator is single-threaded" is now per-worker; the idle-timeout pump writes audit records from its own thread under a handshake | [K129] |
| 003, 004 | PARTIAL | NullSource resume and terminal-only `on_success` superseded in code without banners; redundant resume graph pair | [K131] |
| 006 | PARTIAL | Enforced only for contracts/core/engine; the Violation #11 protocol and its CLAUDE.md pointer are gone | [K024][K119] |
| 008 | PARTIAL | NFR benchmark assertions exist but are deselected in CI | [K031] |
| 009 | PARTIAL | Clause 1: a second (fan-in) aggregation rule is mirrored inline in both walkers; Clause 2: collector closer runs no batch-flush check | [K002][K132] |
| 021 | PARTIAL | Transform boundary set widened to include `IO_READ`, rule version `phase-7a-v3`, ADR still says v1 | [K134] |
| 023 | PARTIAL | SARIF not uploaded to Code Scanning (self-declared) | [K135] |
| 030 | PARTIAL | Two web read paths open writable Landscape engines; PostgreSQL follower join not refused | [K138][K137] |
| 031 | PARTIAL | Backend authoring path complies; the frontend frozen-script mechanics are 27 real `isTutorial` conditionals in 8 files (verified; X2b's 39/12 was an over-count), left for the maintainer to rule on. (R2) The canary premise has been stale since guided was retired as a user mode: freeform has never had a fixed-script canary, and the ADR is unamended | [K139][K160] |
| 040 | CONFORMANT in X2b; at least PARTIAL after R2 | (R2) The composer tool layer fabricates `on_error="discard"` although the runtime has no default for that field, which §3 bullet 2 forbids | [K166] |
| 041 | PARTIAL | No PostgreSQL follower deployment in the catalog, yet `elspeth join` against PostgreSQL is not refused. (R2) Nothing checks the PostgreSQL server version at runtime; the `postgresql-16` profile is AWS-scoped, so this is not a direct violation, but the ACA default is 17 [K154] | [K137][K154] |
| 048 | release notes only (not status drift) | "Proposed" is deliberate until Task 8B lands, and it has not landed. The "non-release" exception text also shipped in 0.8.0. What remains is a Low release-notes gap: the 0.8.0 CHANGELOG promised completion in 0.8.1, and the 0.8.1 section says nothing about ADR-048 or Task 8B | [K028] |
| 026 | prose (G29, D9) | Barrier adoption and marker reset write no `scheduler_events` row. (R2) D9 says the scrubbed payload keeps the payload's identity through its hash, but `payload_hash` hashes an anchor id | [K044][K171] |

**Invariants beyond the ADRs.** Audit primacy holds at all 5 sampled telemetry sites [X2b §B1]. Composer invariant 1 holds: the only `provider="server"` site is a refusal, and a name search for synthesis or fast-path functions returns 0 with a positive control [X2b §B3]. The single server-authored structural insertion is required-control wiring at five seams, an operator-sanctioned carve-out (elspeth-f99655f540) whose sanction is cited only in the module docstring, code comments and a test, not in an ADR or in the AGENTS.md carve-out wording (which speaks of admission gates) [K023][X2b §B3]. The advisor END gate, a completion-blocking control, has no ADR [K082].

---

## 7. Existing-evidence reconciliation

**What existed before this analysis** [X4 §1]: the 0.8.1 web review (79 deduplicated issues: 1 high, 19 medium, 59 low, pinned 70 commits before this pin); the legacy issue tracker (1,333 open+wip rows: P0 0, P1 19, P2 703, P3 485, P4 126; 919 excluding pre-cutover archive rows); 52 curated GitHub issue files; 738 retired code index findings (640 of them secret-scanner hits, 0 architectural); and two governance audits.

**Web review → verified clusters.** 33 clusters cite 35 distinct web-review ids [measured: python3 regex for R01–R73 and G1–G6 ids over cluster titles and refs, extended for the R2 clusters to R-ids in `members` and `provenance` (their `refs` are empty); G-ids in `members` are excluded because they are the critic's gap ids; over titles and refs alone the same regex gives 24 clusters / 28 ids; positive control K001 → R01; the pattern cannot match `R74` or `R00`]. Before the R2 round the count was 21 clusters and 26 ids. Rows marked (R2) come from the R2 round:

| Web review id(s) | Cluster | Verified severity |
|---|---|---|
| R01 (the review's only High) | K001 | Medium — neutral tiebreak: the raw verdict is real but the dispatch boundary re-validates it before the planner or user sees it [K001] |
| R01, R17, R18, R27 | K002 | Medium [K002] |
| R12, R51, R72 | K003 | Medium [K003] |
| R11 | K004 | Medium [K004] |
| R05 | K005 | Medium; confirmed on PostgreSQL, where X4 had it as reasoned-only [K005][X4 §A] |
| R13 | K006 | Medium [K006] |
| R14 | K007 | Medium [K007] |
| R03 | K008 | Medium [K008] |
| R04 | K009 | Medium [K009] |
| R07, R08, R09 | K011 | Medium [K011] |
| R64 | K064 | Medium [K064] |
| R17 | K081 | Medium [K081] |
| R40 | K085 | Medium [K085] |
| R52 | K102 | Medium [K102] |
| R10 | K107 | Medium [K107] |
| R06 | K109 | Medium [K109] |
| R20 | K113 | Medium [K113] |
| R22 | K078 | Low [K078] |
| R33 | K080 | Low [K080] |
| G3 | K139 | Low [K139] |
| R19 (R2) | K182 | Medium: finish-removal recovery lives only in component state and is lost on reload [K182] |
| R35 (R2) | K172 | Low: server-route interpretation rows record `actor='composer-llm'`, sealed into `arguments_hash` on opted-out sessions [K172] |
| R38 (R2) | K173 | Low [K173] |
| R39 (R2) | K174 | Low [K174] |
| R41 (R2) | K175 | Low; its provenance also names R39 and R40 as related [K175] |
| R44 (R2) | K176 | Low [K176] |
| R51 (R2) | K177 | Low; R51 is also cited by K003 [K177][K003] |
| R52 (R2) | K149 | Low: the 500-not-401 defect; R52 is also cited by the Medium K102 [K149][K102] |
| R69 (R2) | K178 | Low [K178] |
| R72 (R2) | K179 | Low; R72 is also cited by K003 [K179][K003] |
| **R02** | **K010** | **Refuted**: epoch 66 makes startup refuse every epoch-65 store, so no supported store reaches the parse sites; R02 was real at `74c0ce0db` and was fixed as a side effect of `4f0e16c6c` [K010] |
| **R15** (R2) | **K180** | **Refuted**: real at the review pin `74c0ce0db`, fixed before the analysis pin by `4afd73169`, which rewrote the notice to promise re-review after the next pipeline change [K180] |
| **R16** (R2) | **K181** | **Refuted**: both halves fixed before the pin by `1809379f6` ("stop the advisor blocker claiming the published reply was withheld") [K181] |

K010 settles what X4 could only call "probably closed incidentally" [X4 §A][K010]. Every other web-review Wave-1 id X4 asked to be re-verified before citing (R01, R03–R09, R11–R13) was re-tested and confirmed present at the pin [X4 §Caveats][K001][K003][K004][K005][K006][K008][K009][K011][K109].

**The three orphan review Mediums (critic gap G9) are resolved.** X4 rated R15, R16 and R19 as medium, and none of them had a cluster before R2. R15 and R16 are refuted because both were fixed between the review pin and the analysis pin, which also settles the S12 vs S10/S22 contradiction on R16 in S12's favour [K180][K181]. R19 is live as the Medium K182 [K182]. So all 20 of X4's Medium/High web-review ids now reach a verified cluster or a verified refutation [X4 §1][measured: python3 parse of the severity cell in X4's per-id tables → 79 ids, 1 high / 19 medium / 59 low, the same as the critic's parse; the 20 medium/high ids (R01–R20) minus the 35 cited ids = 0].

The remaining 44 of 79 web-review issues are not cited by any verified cluster, and X4 rates all 44 as low [measured: the same two instruments]. They are low-severity items that sit, if at all, among the catalog's non-re-verified Low rows, so this document makes no claim about their status [VC §Method][X4 §1].

**Tracker P1s → verified clusters.** 6 of the 19 open P1 rows are cited by a cluster, in 5 table rows because K063 cites two of them (`5887fb7928` and its blocker `d2e3f29d10`) [measured: substring search for the 19 P1 ids over each cluster's full JSON record in temp/verified-concerns.json, re-run after R2: still 6 ids; the only new hit is K160 on `b56bc96c73`]:

| P1 row | Cluster | Note |
|---|---|---|
| `elspeth-5887fb7928` (fixing), blocked by `elspeth-d2e3f29d10` | K063 (High) | X4 recorded "16 plugins"; verification finds 11 still raising at the pin, only `batch_stats` converted [K063][X4 §E] |
| `elspeth-f1a365b714` (fixing) | K109 | part of the multi-query cross-source cluster [K109][X4 §E] |
| `elspeth-b56bc96c73` (pending) | K013; also named by K160 (R2) as an adjacent row | tutorial on the guided lane [K013]; K160 records that no ticket covers the ADR-031 amendment or a freeform frozen walk [K160] |
| `elspeth-da0e3db919` (triage) | K017 | dead or near-dead guided surfaces [K017] |
| `elspeth-225e8df59e` (triage) | K034 | session DML authority split [K034] |

The other 13 P1s are not linked to any cluster here, and no link is invented [X4 §E]: 4 further guided-lane rows (`1318049ffe`, `7578b41719`, `cc1f5e49d1`, `f561d651c8`) wait on the retirement ruling; 3 are epics (`9e57a32e7e`, `21d5691501`, `66e42d5b13`); 1 is tooling (`9258ab4d83`); and 5 are product or governance rows outside the verified set (`e8838c7303`, `edb1b2deae`, `8b7999d1b0`, `cb0d4b8dba`, `fb173ab571`) [X4 §E].

**Tracker vs verified severity mismatches.** K062 is High after verification but its tracker row `elspeth-bbc7000e61` is an open **P3** [K062]. K063's P1 matches its High [K063].

**What is new.** 99 clusters (4 of the 6 Highs: K051, K056, K103, K123) are NEW: neither tracked nor in a dated review [measured: python3 Counter over the leading label of each provenance string in temp/verified-concerns.json][VC §Outcome]. By severity, NEW versus already known (TRACKED, PREVIOUSLY-REPORTED or both) is High 4 / 2, Medium 46 / 46 and Low 48 / 31 [measured: python3 Counter over (final_severity, leading provenance label) in temp/verified-concerns.json]. The R2 round shifted the balance toward already-known items: 20 of its 31 confirmed clusters were already TRACKED or PREVIOUSLY-REPORTED, and 11 are NEW [measured: same Counter restricted to K149–K182]. Ten of the 20 known ones are 2026-09-23 web-review ids (K149, K172–K179, K182) [VC §Outcome]. Several R2 provenance strings are mixed, for example K161's egress framing and K162's orphan-turn mechanism are NEW within an otherwise known item, and the leading-label count does not show that [K161][K162]. The strongest cross-source signal X4 found, the multi-query LLM contract cluster across S10–S12 and S22, is confirmed by three Mediums [X4 §E][K002][K081][K109], with K064 (R64) an adjacent multi-query defect [K064].

**The 2026-09-07 web-split analysis** proposed a compiler facade (`CompiledPipeline`), generated API types and a reconciled decoder layer; at the pin the facade is not built, `types/api.ts` is still hand-written, and the API surface has grown [X4 §B]. The unchecked-cast Tier-3 boundary it flagged is a verified Medium [K033].

**Governance** [X4 §F]: the CI injection of the operator HMAC key (F-01) is gone at the pin; release branches remain unprotected, there is no CODEOWNERS and 0 required approvals (F-07–F-10), and there is no PR process or disclosure channel (F-11, F-24). With more developers incoming, these compound the unenforced layering in §3 [K024].

---

## 8. Strengths

- **`contracts` is a true leaf and the core layering holds.** 0 outbound edges from contracts, 0 upward edges from core or engine, telemetry isolated; the L1 rule's mutation controls fire [X1 §1.2][X1 §5.1].
- **The import-time module graph is acyclic**, and role layering is 98.3 % already present in web without any gate driving it [X1 §0][X1 §2.7].
- **ADR-047 DB-clock authority conforms.** Every deadline issuance in `core/landscape` derives from a database sample; the gate test passed 499 of 499 at the pin [X2b §Part A].
- **ADR-048 token fencing conforms at 89 of 91 mutation APIs** (65 `CoordinationToken` + 24 `WorkerMembershipToken`, required and keyword-only); the other 2 are named establishment exceptions; the fencing gate passed 721 of 721 [X2b §Part A].
- **The declaration-contract registry is sound.** 7 contracts across 4 dispatch sites; the per-site manifest equals the registry and is asserted at bootstrap before both registries freeze; `prepare_for_run()` succeeds live [X2a §2]. The one gap is the collector closer [K132].
- **The terminal-outcome model conforms exactly**: 3 `TerminalOutcome` × 16 `TerminalPath`, 14 legal pairs, closed-set partition checked at import, `completed` XOR `outcome IS NULL` checked on write and on read [X2a §2].
- **Audit primacy holds** at all 5 sampled telemetry emissions [X2b §B1], and Tier-1 exception handlers sampled in `core/landscape` re-raise rather than absorb [X2b §B2].
- **Composer invariant 1 holds**: no server-authored structure apart from the sanctioned required-control carve-out [X2b §B3][K023].
- **38 whole-tree gates plus a mock-discipline baseline** make architecture a tested property, and CI selection policy is itself pinned in tests [X3 §3.2][X3 §6][X3 §8.2].
- **ADR-032 is nearly clean**: 24 `runtime_checkable` Protocols, only one production site uses `isinstance` on one as a control [X2b §Part A][K136].
- **Verification discriminated.** 5 clusters refuted (3 of them in the R2 round), 9 of 14 reported Highs downgraded and 168 of 226 individual votes adjusted the claim text, so the surviving findings are not a rubber stamp [VC §Outcome].

---

## 9. Limitations of this assessment

- **Residual catalog error.** Validators re-checked a sample of 369 catalog claims across the 25 entries and found 52 wrong (14 %) (411 / 62 including the 01-discovery validation); a comparable residual rate should be assumed among unchecked claims, so every §4 size tagged only to a catalog section is ± [00 §Execution Log][VC §Method].
- **Low rows not re-verified.** The 241 Low rows in the catalog's concern tables were not re-tested in the first pass; only High and Medium rows and cross-cut gaps went through verification [VC §Method]. The R2 round later verified a targeted subset (see "Low rows only partly swept" below).
- **No planted-false-claim control.** The verification pass cannot show it *would* have refuted a false claim; the confirmations rest on each verdict's cited evidence, not on the confirmation rate [VC §Outcome].
- **Single-verifier Mediums.** 104 tier-B clusters had one verifier; only the 44 tier-A clusters had two [VC §Method]. The 34 R2 clusters (31 confirmed, 3 refuted) also had one neutral verifier each, so all 8 R2 Mediums rest on a single verdict [VC §Method][VC §Outcome].
- **Low rows only partly swept.** The R2 round verified the catalog Low rows whose web-review ids recurred across slices, and the behavioural baseline-delta rows [VC §Method]. It did not re-verify the remaining catalog Low rows.
- **Frontend read by fewer slices.** The frontend, 69,903 production lines of TS/TSX plus 132,755 test lines, was covered by three slices, against about ten for the web backend [01 §2][01 §6][measured: git ls-files split, §4.1]. Their explorers reported Medium-High (S21), High (S22) and Medium overall, Low for peripheral-panel internals (S23) [S21 §Confidence][S22 §Confidence][S23 §Confidence]. Tests were in scope as structure only, so the frontend and gateway test trees were examined for structure and counts, not read [00 §Analysis Configuration].
- **Read depth beyond the frontend (critic gap G5).** Several backend, deployment and enforcement slices also read important files only in part. Each slice records this in its own §Confidence section, and a finding that rests on an outlined-only file carries that limit. The table lists the partial reads that matter most for this assessment. It is not a complete inventory.

| Slice | Explorer confidence | Read only in part, or not read | Why it matters here |
|---|---|---|---|
| S24 Enforcement | Medium-High | 19 of the 24 rule implementations at metadata and description level only; `elspeth-lints` `core/cli.py` about 1,000 of 6,110 lines [measured: wc -l elspeth-lints/src/elspeth_lints/core/cli.py at the pin → 6,110]; `judge.py` sampled; 6 workflows at trigger level only; `build-push.yaml` trigger and gate only; `scripts/state_engine_*`, `codex_judge_tools.py` not read [S24 §Confidence] | §1's Enforcement rating rests on CI-form re-runs and the live registry (measured), not on reading every rule body [S24 §Confidence][K116] |
| S20 Deployment & ops | Medium-High | 19 of the 23 `_aws_ecs_acceptance` modules outlined only; every Terraform and bicep file outside the listed ones not read (IAM templates, variables, `environment.bicep`); no live-cloud behaviour verified [S20 §Confidence] | Two of the six Highs are deployment defects [K103][K123]; K154's ACA version default comes from the verifier's own read [K154] |
| S06 Engine row processing | Medium-High | The seven declaration-contract modules beyond their outlines; most of `executors/sink.py` (`_write_primary_effect`, diversion handlers); `sink_effects.py` internals; runtime consequences of C1/C3 INFERRED [S06 §Confidence] | K051's consequence was reproduced by verification, but the lease-recovered path was not executed [K051] |
| S03 Landscape DB/schema | Medium-High | `lineage.py`, `reproducibility.py`, `export_mappers.py`, `batch_lineage.py` and others not read; `query_repository.py` structure only; `exporter.py` 1–690 sampled [S03 §Confidence] | K040's export-completeness claim was verified at the pin, independently of the slice read [K040] |
| S13 Guided lane | Medium | `post_guided_respond` respond-action dispatch, `guided_chat_intent_management.py` and `guidedDecoder.ts` bodies not read; that the tutorial never uses deferred-intent management is INFERRED [S13 §Confidence] | The guided-retirement programme in M4 depends on that inference [K013][K016] |
| S14 Sessions domain | Medium | About 1,500 of 15,006 lines of `service.py` sampled; about 1,900 of 4,080 lines of `models.py`; `protocol.py` and `schemas.py` bodies outlined only [S14 §Confidence] | §4.3's `sessions/service.py` decomposition is sample-based except where a K-id re-measured it [K083][K016] |
| S10 Composer core loop | Medium | Advisor body, middle of `run_tool_batch`, `_plan_pipeline_inner` body and `state.py` validation bodies sampled or outlined [S10 §Confidence] | Sizes in M8 are re-measured by verification [K074][K075] |
| S17 Web coordination | Medium-High core; Medium governance writers | `identity_authority.py` (3,307 lines) and the other governance authorities outline and header only; about 3,000 lines of `repository.py` facets signatures only [S17 §Confidence] | §3's coordination ↔ sessions cycle is measured, not read [K034] |
| S19 Web supporting domains | Medium | About 1,100 of 4,455 lines of `blobs/service.py`; `audit_readiness/service.py` row builders and `coverage.py` middle not read [S19 §Confidence] | K100 (blob writers) and K159 (tutorial readiness row) were verified at the pin; the unread regions were not reviewed for further issues [K100][K159] |
| S23 Frontend workspace & shell | Medium overall; Low for peripheral panels | Bodies of 13 peripheral panels, most E2E spec bodies and the admin sections not read [S23 §Confidence] | K182 was verified in R2 directly, not from the slice [K182] |

- **retired code index is stale and uninformative.** Its index is at `ee04378f8`, its last run failed on an entity cap, its "0 import cycles" comes from an empty import-edge set, and its 738 findings contain no architectural ones [X1 §4][X4 §D].
- **Suites not run.** No full test suite was run for this analysis; test statements are structural and measured by collection or grep [X3 §Caveats & Required Follow-ups].
- **Inferred mechanisms.** Where a cluster says a consequence was not executed (for example the lease-recovered path in K051, the RSS link in K049), this document inherits that limit [K051][K049].
- **Tracker attribution.** 36 % of tracker rows name no file, so tracker volume per subsystem is an attention heuristic, not a quality score [X4 §Risk Assessment].

---

## Validation corrections

Applied by the validation gate on 2026-09-23 (report: `temp/validation-05-quality.md`). Coverage and severity fidelity passed mechanically: all 6 High, 84 Medium and 56 Low K-ids are present, every table row's severity, provenance, title and slices match `temp/verified-concerns.json`, and the refuted K010 and K045 appear only as refuted. The corrections below fix claims where the tag was wrong, or where the text followed a cluster *title* or an unverified cross-cut figure rather than the verified claim.

1. **Header, Baseline line.** The tag `[01 §2]` for "ARCHITECTURE.md last updated 2026-09-11" pointed to a section that does not state the date; it is in 01's header, which is not a taggable section. Replaced with a `[measured]` tag for `ARCHITECTURE.md:5`.
2. **§1, Enforcement row.** "masks at least 3 independent live regressions [K116]" followed K116's title. K116's verified claim says the 3 red steps include only one real drift (the fingerprint mismatch), and the other two are lint false positives. Rewritten to that.
3. **§2.2 intro.** Added K116 and K144 to the list of clusters whose verified claim corrects the title, and retagged the rule to `[VC §Outcome]`, which is where VC states it.
4. **§2.2 M10 intro.** "static-analysis masks live regressions behind the first red [K116]" was rewritten to K116's verified claim: it skips 9 later steps and hides three red steps, one of them real drift.
5. **§3, role layering.** "10 of them from two misfiled Azure acceptance modules and 21 from domain → service [X1 §2.7][K144]" repeated X1's figures, which K144's verified claim corrects to 11 misfiles, 2 from `shareable_reviews/__init__` and 20 domain → service. Rewritten to the verified figures.
6. **§5, unit-only packages.** The claim is now attributed as X3's list, and it records that X3's own annotations give `mcp` and `web.shareable_reviews` one integration file each.
7. **§7, P1 table.** The text said 6 P1s and showed 5 rows. Added that K063's row carries two P1s (`5887fb7928` and its blocker `d2e3f29d10`). The measured count of 6 is correct.
8. **§9, residual catalog error.** Removed "prioritised". Neither 00 nor the validation reports describe the 411-claim sample that way.
9. **§9, frontend depth.** "three slices whose explorers mostly reported Medium confidence" misread the tagged sources. S21's section says Medium-High, S22's says High, and S23's says Medium overall. Rewritten to those values, keeping the slice-count contrast from 01 §6.
10. **§1, enforcement note.** "a place where real regressions accumulate unseen [K116][K117][K118]" became "drift accumulates unseen and gate signal is lost". K116 finds one real drift, K117 says "accumulating drift" and K118 says "lost signal".

---

## Revision R2 (post-critic)

Applied on 2026-09-24. Inputs: `temp/validation-completeness-critic.md` (gaps G1–G18) and `temp/verified-concerns.json`, which after the R2 gap round (`wf_8c2f2711-89f`, 37 agents) holds 182 clusters: High 6 · Medium 92 · Low 79 · refuted 5 [VC §Outcome][00 §Execution Log]. No severity was re-rated, and no refuted cluster is presented as live. The "Validation corrections" section above is unchanged. Its figures (6 High, 84 Medium, 56 Low, K010 and K045 refuted) describe the document before this revision.

**Gap fixes.**

1. **G1, §4.1.** "The 202,658-line frontend" became 69,903 production lines of TS/TSX in 259 files plus 132,755 test lines in 338 files. The split was re-measured at the pin with `git ls-files` (total 202,658, matching the critic's two-classifier result; positive control `stores/sessionStore.ts`).
2. **G1, §9.** The frontend-depth bullet now uses the production/test split and says the test trees were examined for structure only (this also covers G17).
3. **G1, §5.** Added a note that X3's frontend source count (264 files / 70,062 lines) comes from a slightly different classifier than the corrected 259 / 69,903. X3's figure is kept under its own tag.
4. **G5, §9.** Added a "Read depth beyond the frontend" bullet and a per-slice read-depth table covering S24, S20, S06, S03, S13, S14, S10, S17, S19 and S23. Each row is taken from that slice's §Confidence section.
5. **G5 and R2, §9.** Updated the single-verifier bullet: the 34 R2 clusters each had one neutral verifier. Added a "Low rows only partly swept" bullet, and noted in the "Low rows not re-verified" bullet that R2 verified a targeted subset.
6. **G10, §6.** Reworded the counting rule and the ADR-048 row to follow K028's verified claim. "Proposed" is not drift, because Task 8B has not landed. What remains is a Low release-notes gap. The row's verdict is now "release notes only (not status drift)", and the table intro was reworded to match [K028].
7. **G3 via K170, §2.2 M5.** Added a note on LLM tracing egress, built from K170's verified claim, not from the critic's wording. Web denies tracing. Langfuse on YAML/CLI sends prompt and response content verbatim, is opt-in and is documented. The critic's `azure_ai` half is refuted: the path fails at `on_start`, and its content switch governs an SDK the provider never calls [K170].
8. **G9, §7.** Recorded that the three orphan review Mediums are resolved. R15 is refuted as K180 and R16 as K181; both were fixed before the pin. R19 is live as K182.

**R2 integration.**

9. **§2.2, 8 new Mediums**, each in one theme, with the theme header count and intro prose updated:
   - M1 +K155 (now 11)
   - M3 +K166 (14)
   - M4 +K160 and K162 (9)
   - M5 +K182 (9)
   - M6 +K150 (8)
   - M9 +K154 and K161 (8)

   The §2.2 intro now reads 92 of 92. It also explains that R2 provenance shows only the leading label, and that the Slice(s) column gives R2 unit ids (D*nn*, G9-R*nn*).
10. **§2.3, 23 new Lows.** Updated the theme table (counts, K-ids and descriptions) and the "Titles, for scanning" bullets:
    - Guided and tutorial residue +K159 (6)
    - ADR and documentation drift +K152, K156, K165, K167, K171, K176, K179 (18)
    - Engine, Landscape, audit and data-path correctness (renamed) +K153, K172, K175 (14)
    - Composer, web and frontend robustness (renamed) +K149, K151, K169, K173, K174, K178 (12)
    - Security hygiene +K158, K170 (7)
    - Enforcement and test infrastructure +K164 (7)
    - Deployment, operator surfaces and satellites (renamed) +K157, K168, K177 (6)

    Structure and layering is unchanged (9). The §2.3 intro now reads 79 and explains where the 23 R2 Lows came from.
11. **§2.3 Refuted.** Now lists all five refuted clusters: K010, K045, K163 (D15), K180 (R15) and K181 (R16) [K163][K180][K181].
12. **§1 Calibration.** Updated to 6/92/79/5 of 182, with the R2 origin of K149–K182. Provenance was recounted by leading label (NEW 99, TRACKED 31, REPORTED-AND-TRACKED 16, PREVIOUSLY-REPORTED 36), with a note on why VC's exact-string tally shows NEW 95. "All 182 unchanged after the pin".
13. **§1 ratings.** No rating changed. Added K155 to the Audit row evidence and K150 and K154 to the Operability row evidence, plus a paragraph explaining why the 8 new Mediums move no dimension. The Documentation row now gives 28 conformant ADRs once K166 is applied [K166].
14. **§6, R2 clusters.** Added a paragraph mapping R2 clusters to ADR rows: K154 → 041, K160 → 031, K165 → 010, K171 → 026. K155 conforms to the wording of ADR-034, and the gap is against audit primacy. Added a new ADR-040 row for K166. X2b had rated ADR-040 CONFORMANT, but its "no fabricated defaults" clause was marked not exhaustively verified. With K166 applied, the totals would be 28 CONFORMANT and 12 PARTIAL. The scorecard keeps X2's totals so that it stays traceable.
15. **§7 web review.** Re-measured to 33 clusters citing 35 ids (it was 21 and 26). The regex was extended to R-ids in the R2 clusters' `members` and `provenance`, excluding the critic's G-ids. The controls are recorded in the tag. Added 12 rows marked (R2): R19 → K182, R35 → K172, R38 → K173, R39 → K174, R41 → K175, R44 → K176, R51 → K177, R52 → K149, R69 → K178, R72 → K179, R15 → K180 (refuted) and R16 → K181 (refuted). The uncited remainder is now 44 of 79, all of them low.
16. **§7 P1 table.** Re-ran the P1 substring search over the full cluster records. The count is still 6 ids, and the only new hit is K160, which names `b56bc96c73` as an adjacent row [K160].
17. **§7 What is new.** Recounted by leading label to NEW / known = High 4/2, Medium 46/46 and Low 48/31. Added the R2 split: of the 31 confirmed R2 clusters, 20 were already known and 11 are NEW. Noted that some provenance strings are mixed [K161][K162].
18. **§8.** Updated "Verification discriminated" to 5 refuted (3 in R2) and 168 of 226 votes [VC §Outcome].
19. **§7 G9 claim instrument.** "All 20 Medium/High ids reach a cluster" and "the remaining 44 are all low" are now tagged to a parse of X4's per-id severity cells. That parse gives 79 ids, 1 high / 19 medium / 59 low, the same as the critic's, and the 20 medium/high ids minus the 35 cited ids leaves 0.
20. **M3 and M4 prose checked against the full verified claims.** K166 now says "at five production sites". The K162 sentence now carries the claim's reach limits (a terminal guided session, a declined revision with no `chat_messages` twin, an exact re-send; server and audit unaffected). K160 now says the freeform gap predates the retirement [K160][K162][K166].
21. **§9 S24 row.** Named the lints file as `core/cli.py` and measured it (6,110 lines at the pin).
22. **Header.** Added "revised 2026-09-24" to the Date line.

**Row fidelity check.** A second script parses every §2.2 row as `| [Knnn] | title | severity | provenance | slices |`. For all 92 rows it compares the title with the JSON `title`, the severity with `final_severity`, and the provenance with the leading label of the JSON `provenance`. For R2 rows it also compares the slices cell with `R2: ` plus `members`. Result: `rows checked 92 mismatches []`. Control: changing one character of K155's title is detected.

**Coverage check (re-run after these edits).** `scratchpad/chk05.py` parses every `| [Knnn] |` row in §2.2, the K-id column of the §2.3 theme table and the §2.3 title bullets. It compares them with `temp/verified-concerns.json` and also checks each theme's stated count against its rows. Result on this file:

```
Medium rows: 92 distinct 92 dups []
  missing [] extra []
Low theme ids: 79 distinct 79 dups []
  missing [] extra []
Low title bullets: missing [] dups [] extra []
refuted in themed sets: []
theme count mismatches: [] M header mismatches: []
RESULT PASS
```

Negative controls on mutated copies all went red. Mutation 1 removed the K155 row, and the check reported `missing ['K155']` and an M1 header mismatch. Mutation 2 removed K170 from the theme table, and it reported `missing ['K170']` and a Security count mismatch. Mutation 3 duplicated the K150 row, and it reported `dups ['K150']`. Each mutation ended in `RESULT FAIL`. A separate check found every one of the 182 cluster ids cited in the body above the validation sections, with no unknown ids.

---

## Validation corrections (R2)

Applied by the R2 re-validation on 2026-09-24 (report: `temp/validation-R2-05-quality.md`). The R2 revision above was checked against `temp/verified-concerns.json`, the critic's gap list, the slice §Confidence sections, X2b, X4 and the pin. No false claim was found in the R2 changes. One sentence paraphrased X2b's scope more widely than X2b states it (item 1). The Revision R2 section above is left as the editor wrote it. The edits below are one precision fix, one clarification and two format fixes.

1. **§6, R2 paragraph (ADR-040).** The text said X2b marked ADR-040's "no fabricated defaults" clause as not exhaustively verified. X2b's own wording is narrower: "No fabricated defaults in the YAML generator" was not exhaustively verified (INFERRED), and ADR-040's Stage-1 rules were not audited one by one [X2b §Part A]. K166's five fabrication sites are in the composer tool layer and the guided planner (`tools/sessions.py`, `tools/transforms.py`, `guided/planning.py`), not the YAML generator [K166]. The sentence now quotes X2b's scope and says that K166's sites fall outside it. Revision R2 item 14 above carries the older wording.
2. **§6, counting rule (G10 follow-through).** Added one clause. K027's verified claim still lists ADR-048's "Proposed" among its stale-baseline items, while K028's says it is not drift. The §2.3 K027 bullet and the §6 rule would otherwise look contradictory. The clause says this document follows K028, the cluster about ADR-048 itself [K027][K028].
3. **§6, format.** Added a blank line between the ADR table's last row (026) and "**Invariants beyond the ADRs.**". Without it GFM reads the paragraph as a row of the table.
4. **§9, format.** Added a blank line between the read-depth table and the "retired code index is stale" bullet.
