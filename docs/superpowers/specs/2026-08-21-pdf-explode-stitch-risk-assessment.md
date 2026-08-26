# PDF explode → per-page Textract → stitch: synthesized risk assessment

Ticket: elspeth-0038a69467 (P2 bug) · gate: elspeth-bc566ed043 (resolved) · sibling: elspeth-4139923015
Date: 2026-08-21 · Branch: release/0.7.2 · Inputs: 5 unit plans + 5 risk lenses (independent agents)

---

## 1. Verdict

**(a) No — this is no longer the small additive job the ticket assumed.** The gate came back
`NO_NOT_OBSERVABLE`, and Correction 2 promotes the batch failure-sweep from a stitcher detail to a
universal engine capability. The honest shape is three pieces of work, not one: a universal
expand-group-completeness capability with an engine arm (large), the Textract-specific
`pdf_rasterize` + stitcher + hints (large + large + small), and a worked example (medium). "ZERO
engine changes" is dead.

**(b) The single biggest risk is that the primary use case — one PDF, one page fails — aborts the
run today, and the fix is engine work on a shared component on a release branch.** A transform-mode
aggregation has no legal zero-emission outcome: `ZeroEmissionSuccessContractViolation`
(`engine/executors/can_drop_rows.py:143`) fires before routing. MEASURED — see §2. Second-ranked
risk (a boot-kill from a module-scope renderer import) has a one-line fix and is not co-headline.

**(c) The SES composer demo is NOT exposed as shipped — MEASURED.** `REQUIRED_WEB_PLUGIN_IDS` is 13
ids with no `aws_*` entry (`web/plugin_policy/compiler.py:17-39`), `plugin_allowlist` defaults to
`()` (`web/config.py:442`), and `deploy/elspeth-web.env` sets no `ELSPETH_WEB__PLUGIN_*` variable.
Three conditional exposures survive and are listed in §5(e); the sharpest is that fixing
`aws_textract_document_analysis`'s own hints changes nothing the profiled composer sees.

**(d) It can land on release/0.7.2, but not as one commit and not in a demo window.** Units 2–5 are
additive and gate-priced. Unit 1 touches `engine/executors/aggregation.py`, `engine/processor.py`
and `core/config.py` — shared machinery whose two relevant guards
(`engine/executors/aggregation.py:549`, `:554`) have **zero test references anywhere in `tests/`**.
Do not schedule that change and an SES demo in the same window (ADR-031: the tutorial's run step
executes the engine, and it is the surface that dies first on engine defects).

---

## 2. The gate, resolved — and one correction to the gate resolution itself

**The literal answer stands.** A failed upstream member is ABSENT, not observably-failed: the token
terminates in `handle_transform_error_status` (`engine/token_traversal.py:288-388`), no continuation
is minted, the barrier never adopts it, and `PipelineRow` (`__slots__ = ("_contract","_data")`)
carries no failure marker. `AggregationParentDisposition` is write-side only
(`engine/aggregation_result.py:54-70` *constructs* it from arrived tokens). Adversarial
verification: 1/2 refuters dissented; the answer stands.

**The rescue is real but narrower than stated.** Under `trigger: {}` the END_OF_SOURCE flush is
refused unless the scheduler journal is quiesced (`engine/orchestrator/leader_drain.py:453-462`), so
"missing" unambiguously means "failed or never produced". But that gate **raises**
`OrchestrationInvariantError` rather than waiting, and its own docstring (`:427-430`) says the
multi-worker wait loop "lands with the worker-pool slices (4/5)" — i.e. is not implemented. Fail-loud,
so the sweep stays sound, but **the deployment running this pipeline is N=1 until those slices land.**

**Consequence item #4 of the gate resolution is WRONG, measured.** The resolution asserted
`TransformResult.success_empty()` was "verified legal end-to-end" via the zero-output early return at
`engine/processor.py:1558-1559`. That early return exists — but it is never reached. I read the
ordering directly:

- `validate_success` → `_cross_check_flush_output` is a **precompletion callback passed into**
  `AggregationExecutor.execute_flush` (`engine/processor.py:1776-1782`).
- `_cross_check_flush_output` computes `used_success_empty = result.rows is not None and
  len(result.rows) == 0` (`:1181`) and dispatches `run_batch_flush_checks` unconditionally (`:1270`).
- `CanDropRowsContract.applies_to` (`engine/executors/can_drop_rows.py:109-116`) covers
  `not passes_through_input` — the base default (`plugins/infrastructure/base.py:420`).
- `_verify_zero_emission_success_path` (`:118-159`) early-returns only when `passes_through_input` is
  True (`:145-146`); otherwise it **raises** `ZeroEmissionSuccessContractViolation` (`:148`).
- `_prepare_transform_route` (`:1599`, early return at `:1558`) runs only after all of that.

So the abort wins on ordering. Four independent sources measured the same thing (phase-2 refuter #2,
`stitching_aggregation` STEP 0, `sibling_batch` lens finding 1 — which ran
`tests/unit/engine/test_cross_check_flush_output.py`: 13 passed, all four parametrized cells assert
the violation — and `universal_completeness` STEP 0). This is settled, not a disagreement.

**Why it matters and what it does NOT require.** The all-quarantined case *is* the single-PDF demo
shape. But the escape is coherent and needs no ADR-012 relaxation and no contract carve-out: the
engine arm proposed in `universal_completeness` STEP 8 **never invokes the plugin**, so
`validate_success` is never called with a plugin result and no declaration is under test. The
difference is "needs an engine arm" (real, scoped) versus "needs a contract relaxation" (would have
been much worse).

**Estimate impact:** the gate closed the *read-side* question but opened the *emission* question.
Unit 1 grows from "expose a fact" to "expose a fact + filter the flush + a new engine commit arm".

---

## 3. The work — five units, in build order

Estimates are the planning agents' own, adjusted where Correction 3 moved them.

### Order 0 — the probe test (hours, not days). Do this first.
One integration fixture, sibling of `_MixedEmptyBatchTransform`
(`tests/integration/pipeline/test_aggregation_recovery.py:171-181`), that quarantines **every**
buffered index under `output_mode: transform` + `trigger: {}` and records what actually happens.
`test_aggregation_recovery.py:742-745` deliberately parametrizes only
`[(transform,False),(transform,True),(passthrough,False)]`, and its True arm quarantines index 1 of 2
— a strict subset. `grep -rn 'all buffered tokens were quarantined|passthrough aggregation cannot
declare' tests/` returns **zero hits**: neither guard (`engine/executors/aggregation.py:549`, `:554`)
is referenced by any test. This one fixture de-risks every unit below and costs almost nothing.

### Order 1 — Unit 1: universal expand-group completeness. **LARGE.** No dependencies.
Recommended shape (B+): the engine derives completeness and enforces the sweep inside
`AggregationExecutor`, exposing the fact read-only. Two tiers.
- **Tier 1** (fact, zero plugin change): `ExpandGroupCompleteness` owned type; a durable read on the
  already-indexed `tokens.expand_group_id` (`core/landscape/schema.py:613`, modelled on
  `core/landscape/data_flow/tokens.py:589-640`); computed in `RowProcessor` and passed *into*
  `execute_flush`; exposed as `ctx.expand_group_completeness` following the `ctx.batch_token_ids`
  precedent (`engine/executors/aggregation.py:454`, `:710`). Tri-state
  `COMPLETE|TERMINALLY_INCOMPLETE|INDETERMINATE` — a bare count is the inference trap the
  carry-it-structurally doctrine exists to prevent.
- **Tier 2** (enforcement, config-gated): `AggregationSettings.on_incomplete_expand_group:
  ignore|quarantine`, rejected at config time unless `trigger` is empty AND `output_mode: transform`;
  a single filter point in `_snapshot_flush_inputs` (`:399-421`); and the new engine commit arm for
  the all-incomplete case (§2).
- Files: `contracts/expand_completeness.py` (new), `contracts/plugin_context.py`,
  `core/landscape/data_flow/tokens.py`, `core/landscape/data_flow_repository.py`, `engine/tokens.py`,
  `engine/processor.py`, `engine/executors/aggregation.py`, `engine/aggregation_result.py`,
  `core/config.py`, plus the five composer plumbing points (§5e).

### Order 2 — Unit 2: `pdf_rasterize`. **LARGE.** Blocked on ONE developer decision (renderer).
Blocked on nothing in unit 1 for its own correctness, but **must be re-briefed first** (see the
coordination note below). No registry edit exists: `PLUGIN_SCAN_CONFIG`
(`plugins/infrastructure/discovery.py:287-291`) already lists `"transforms"`, so dropping the file in
registers it. Files: the plugin, a worker seam outside any scanned directory
(`plugins/infrastructure/rasterize/`), `pyproject.toml` + `uv.lock`, and the whole-tree pin inventory
in §5(c).

### Order 3 — Unit 3: the stitcher. **LARGE** as briefed; **MEDIUM** if unit 1 Tier 2 lands first.
The fork is real and it is the single biggest driver of this unit's size. If the engine filters
incomplete groups before `process()` is called, the stitcher is config + grouping + emit. If the
plugin must derive incompleteness itself, the arithmetic lands here — which is exactly what
Correction 2 forbids. **Recommend engine-computed.**
Scope note that is not a widening of intent: the correct design is a plugin **plus a third public
entry point** in the shared normalizer, `normalize_stitched_pages` in
`plugins/transforms/aws/textract_result.py`. Reason: the two Textract plugins are contractually
required to emit identical shapes (`textract_inline_analysis.py:5-8`, elspeth-0c6a343921), and
`_normalize_block_graph`'s own docstring (`textract_result.py:710-718`) argues the graph rules must
not drift between callers. Hand-stitching six facets guarantees drift.

### Order 4 — Unit 4: composer hints. **SMALL.** Must land WITH or AFTER units 2 and 3.
A hint naming a plugin that does not exist passes every gate in the tree
(`tests/unit/plugins/test_composer_hint_config_consistency.py:16-20` explicitly declines to check
references to things that do not exist). Four plugins' prose, plus two out-of-plugin surfaces
(`web/composer/tools/sources.py:1033-1039`, `web/plugin_policy/profiles.py:1188-1196`).

### Order 5 — Unit 5: worked example. **MEDIUM.** Blocked on unit 1 + a generic stitcher only.
`examples/expand_reassemble/` is AWS-free and PDF-free: `json` → `json_explode` → `type_coerce` →
reassembly aggregation → sinks, plus a `settings_incomplete_group.yaml` variant that exits 1 by
design. The agent validated this topology in-process (build-only). It is the acceptance artifact for
the whole design. The Textract half is a *phase 3* extension of the existing
`examples/textract_inline/` directory (which already carries the credentials gitignore and the corpus
exemption at `tests/e2e/examples/test_shipped_examples.py:69-77`), not a new directory.

### Coordination note — re-brief before units 2 and 3 start
Unit 1's recommendation (STEP 11) **deletes** "stamp `page_count` for gap arithmetic" from
`pdf_rasterize`'s requirements and **deletes** the sweep from the stitcher entirely. `document_id`
and `page_index` survive, for a different and honest reason: grouping and ordering keys, plus
per-document provenance (which is recoverable only from row data — see the lineage finding). If units
2/3 start against the old brief they will build the shared invariant into a specialised component.

---

## 4. Risk register

| Sev | Finding | Blast radius | Mitigation |
|---|---|---|---|
| **Blocking** | **All-quarantined aggregation flush aborts the run.** `success_empty()` from a `passes_through_input=False` transform-mode aggregation raises `ZeroEmissionSuccessContractViolation` (`can_drop_rows.py:143`, applies_to `:109-116`, dispatched `processor.py:1181`+`:1270`, ordered before routing via `processor.py:1776-1782`). `success_multi([])` raises `ValueError` (`contracts/results.py:423-424`). This is the single-PDF demo shape. MEASURED by 4 agents + my own read; **the gate resolution is wrong on this point.** | Any transform-mode aggregation, all 13 batch consumers by shape (12 are `passes_through_input=False` by base default `base.py:420`). Fix lands in `engine/executors/aggregation.py` on a release branch. | Unit 1 STEP 8's engine commit arm — the engine emits zero rows and commits the receipt **without invoking the plugin**, so no declaration is under test and no contract carve-out is needed. Pin with the Order-0 probe test and a mutation test on the `processor.py:1558` vs `:1570` guard ordering. |
| **High** | **A module-scope renderer import kills app boot for every ELSPETH process**, including the live demo server. `discovery.py:88-99` skips `ModuleNotFoundError` only for a closed 4-module allowlist (`:18-25`, `{bs4, chromadb, html2text, jinja2}`) and `except ImportError: raise` (`:98-99`) re-raises unconditionally; propagates through `manager.py:78-83` into `CatalogServiceImpl.__init__` (`web/catalog/service.py:222-231`). `deploy/elspeth-web.service` serves this working tree from this venv. | Every CLI and web process; the demo box. | Lazy import inside the render seam, or a subprocess renderer (no import to fail). Do **not** add the module to `OPTIONAL_PLUGIN_IMPORT_MODULES` — the plugin then silently vanishes from the catalog and the 33/51/72 count gates become environment-dependent. |
| **High** | **In-process native PDF parsing runs untrusted bytes inside the credential-holding process.** ELSPETH already rejects this pattern for a strictly smaller surface: `plugins/transforms/rag/query.py:44-51` ("process-level isolation is the only reliable timeout mechanism"), implemented at `:79` with `mp_context=mp.get_context("spawn")` and orphan-kill at `:143-159`. | uvicorn web process (`deploy/elspeth-web.service:7,28-34,40-55` — **no** `MemoryMax`/`TasksMax`); ECS task envelopes (`deploy/aws-ecs/.../ecs.tf:153-154`). | Mandate out-of-process rendering: spawn-context `ProcessPoolExecutor`, `max_tasks_per_child=1`, **one task per DOCUMENT** (parse once, render many), per-document wall clock, `RLIMIT_AS`+`RLIMIT_CPU` in the worker initializer. `setrlimit` appears **nowhere** in `src/` today — this is a genuinely new convention and belongs in `docs/agents/recent-code-hints.md` in the same commit. |
| **High** | **No page-count, pixel, or page-byte cap is specified.** `grep -rn "max_expansion\|expansion_limit\|max_children"` over `src/elspeth/engine/` returns **zero** — the engine provides no expansion backstop. A 5 MiB PDF may declare 100k pages → 100k tokens, 100k payload writes, 100k billable Textract calls. A declared MediaBox of 200in at 300 DPI is ~14 GB of bitmap from a few dozen bytes of input. | Payload store (`core/payload_store.py:210` `store(content: bytes)` — no size or aggregate quota), Textract spend, RSS. | Five caps, each with a module-level `le=` hard ceiling and a typed row error naming observed-vs-cap, following `line_explode.py:39,50-54,278` and `blob_csv_expand.py:30,49,482`: `max_input_bytes`, `max_pages`, `max_page_pixels` (checked from declared dimensions BEFORE the bitmap request), `max_page_bytes` (`le=BINARY_DOCUMENT_MAX_BYTES`, `contracts/binary_documents.py:27`), `render_timeout_seconds`. |
| **High** | **The renderer lands in base `dependencies`, not an extra.** `pyproject.toml:75-92` states the rule verbatim: "if a non-optional unit test references a plugin, that plugin's deps live here" — and the catalog/discovery/golden pins ARE non-optional tests. Every ELSPETH install, including CLI-only and non-PDF government deployments, gains a native PDF parser. | Every install path, the Docker image, all 7 CI `uv sync --frozen --all-extras` jobs, the licence report, the CVE audit. | Only the subprocess design escapes (plugin module imports nothing native). Otherwise surface it as an explicit accepted cost — the ticket currently says "first PDF dependency", not "unconditional in every install". This arguably meets the ticket's own trigger, "revisit if the dependency review goes badly" (§6). |
| **High** | **Naive concatenation of per-page Textract results is a guaranteed runtime `MalformedTextractResponse`.** Every sync response is materialized to `Page: 1` (`textract_result.py:668-680`, cites live observation 2026-08-10); the graph core rejects PAGE numbering ≠ `range(1, page_count+1)` (`:736-738`) and duplicate block Ids across the concatenation (`:724-725`). | `plugins/transforms/aws/textract_result.py` — **shared** with `aws_textract_document_analysis`; its 901-line test file must be re-run and extended. | `normalize_stitched_pages`: renumber `Page` from the stamped `page_index`, namespace `Id` and every `Relationships[].Ids` element with a deterministic reversible prefix, then delegate to the existing `_normalize_block_graph`. The duplicate-id and dangling-relationship checks then become a real cross-page proof. Free backstop: the PAGE-numbering check independently catches a gap. |
| **High** | **Latent correctness exposure across the batch family, TODAY** — see §7. Five comparators publish arrival-derived denominators (`batch_effect_size.py:486`, `batch_experiment_compare.py:515`) and cannot distinguish 97-of-97 from 97-of-100. | 13 consumers; the 5 comparators are the high-severity subset. | Unit 1 Tier 1 fixes all 13 with zero plugin change. **Latent, not active**: zero shipped example reaches it (all 13 aggregation settings wire `input:` straight from the source with an empty `transforms:` list), so it does not block the branch — but it raises the capability's priority above P2. |
| **High** | **`docs/agents/recent-code-hints.md` §6 "New-plugin exact inventories" is INCOMPLETE.** It is dated 2026-08-09 and omits at least: `web/audit_readiness/boundary_expectations.py:148-186` (whose own docstring at `:70-77` says "IN THE SAME COMMIT. The parity test will fail otherwise"), the 2026-08-12 PB-09 lifecycle-matrix entry, and the 2026-08-20 `source_file_hash` strict-equality entry. | The branch goes red for every sibling — the exact failure the doc exists to prevent. | Work the union of §6 + the 08-12 and 08-20 entries; add the three missing pins to §6 in the same commit (the doc is explicitly rolling). |
| **Medium** | **A count/timeout trigger downstream of an exploder splits the group and produces FALSE incompleteness** — quarantining a document that never failed, with a clean audit trail. MEASURED: the builder accepts `trigger: {count: 3}` downstream of `json_explode` with no error and no warning; `creates_tokens` appears **nowhere** in `src/elspeth/core/dag/`. The structurally identical hazard downstream of `row_union` is a hard `GraphValidationError` (`core/dag/builder.py:1344-1400`). | Every exploder (`json_explode`, `line_explode`, `blob_csv_expand`, `pdf_rasterize`) × every batch plugin. Reachable from the composer. | Unit 1 STEP 5's config validator and this missing builder guard are **the same hole from two sides** — the validator refuses the config, the builder guard refuses the graph. Land at least one before the shape is reachable in a demo. Worst failure mode available to demonstrate to an SES audience: it validates, it runs, and it is wrong. |
| **Medium** | **Six whole-tree exact-set gates fire on omission, not mismatch.** `EXPECTED_TRANSFORM_COUNT` 33 → **35** (`tests/unit/plugins/test_discovery.py:265`; a unit plan says 33→34 because it scoped only its own plugin — full scope is two new plugins), `len(live_keys)` 51 → 53 and `EXPECTED_VARIANT_COUNT` 72 → 74 (`:300-304`), `EXPECTED_BUILTIN_IDENTITIES` + `DIRECT_CONFIG_REFERENCES` (`test_catalog_reference_content.py:34,213,219,256`), knob-schema golden set-equality (`tests/unit/web/catalog/test_knob_schema_golden.py:39-40`), `EXPECTED_TRANSFORM_DETERMINISMS`, and the PB-09 three-way contract (golden + v3 catalog + derived variant map). | `tests/golden/state_engine/plugin_lifecycle_matrix.json` is read by six modules; `boundary_expectations.py` is a production-code diff. | Same-commit obligations, not follow-ups. Regenerate rather than hand-write; the PB-09 reviewed fields need **manual adjudication** (`render-skeleton` exits nonzero while any is UNCLASSIFIED). Frame the determinism pin precisely: it is a Determinism-keyed audit-discoverability parity pin, **not** a trust-tier claim — Correction 1 stands. |
| **Medium** | **`source_file_hash` goes stale on the hint edit and no local test catches it** (`textract_inline_analysis.py:258`). A stale hash with a mutated body previously passed 10,213 tests. | CI plugin-hash gate; the branch, for every sibling. | Recompute with `scripts/cicd/plugin_hash.py::compute_source_file_hash` **after** `ruff format`, and compare with strict equality (`Cls.source_file_hash == compute(...)`), never a substring check. |
| **Medium** | **The composer silently drops a new `AggregationSettings` field.** `web/composer/yaml_generator.py:298-314` emits a hardcoded allowlist (name, plugin, input, on_success, on_error, then conditionally trigger, output_mode, expected_output_count, options). A composer-authored pipeline would run **without** the quarantine guarantee, no error, no warning. | Five plumbing points: `yaml_importer.py:362-380`, `yaml_generator.py:298-314`, `audit.py:1172/1196/1214`, `capability_skill.py:110-124`, `tools/sessions.py:1117,1355`. | Ship the plumbing in the same change, or gate Tier 2 YAML-only and say so in the hints. This is authoring plumbing, not structure synthesis — **no composer invariant is implicated.** |
| **Medium** | **Per-document lineage collapses.** Under `group_by` + `output_mode: transform`, every emitted row expands from one arbitrary surviving member token (`engine/executors/aggregation.py:546-551`). A refuter measured doc 3's stitched result carrying doc 1's `row_id`. "Which source PDF produced this row" is recoverable ONLY from row data. | Pre-existing and shared with the shipped `batch_stats` examples — but a per-document *result* needs per-document provenance in a way a group statistic does not. | Declare `document_id_field` in `declared_output_fields` and in output `guaranteed_fields` so provenance is contractual, and carry `pdf_source_blob_ref` as a human channel. For an audit-first engine demoed to SES audiences, decide this deliberately rather than answering it live in the room. |
| **Medium** | **A fully-failed document never reaches the stitcher at all.** `flush_remaining_aggregation_buffers` (`engine/orchestrator/aggregation.py:235-237`) reads `buffered_count` and `continue`s on zero — `process()` is not called. No document-level failure record exists; gap arithmetic structurally cannot cover it. Unit 1 does not change this. | Every expand-group whose members all fail. | Not a data-loss defect: each page carries its own FAILURE/QUARANTINED_AT_SOURCE and no partial result escapes. Name it as a limitation in the README and the hints; a per-document failure row must come from the error sink or a reconciliation over the audit trail. |
| **Medium** | **Prose gates constrain the required hint rewrite in both directions.** `tests/unit/plugins/transforms/aws/test_textract_inline_analysis.py:818-823` asserts `"single-page"` and `"aws_textract_document_analysis"` appear in the joined hints; `test_external_catalogue_metadata.py:104-107` pins casefolded substrings (use: synchronous/blob_rows/payload-store/ocr/single-page/5 mib/untrusted before llm; avoid: multipage/s3/billable); `test_catalog_reference_content.py:293-316` requires global uniqueness; `capability_tags` is at the hard 6-tag ceiling. | 4 plugins' prose + 2 out-of-plugin surfaces. | The hints agent has already written replacement strings and mechanically verified them against every gate. Convert `:818-823` from substring-presence to a **claim** test naming `pdf_rasterize` — a declaration test pins existence, not truth, which is exactly how `:729` rotted. |
| **Medium** | **Masquerade and terminal-vocabulary lints scan the WHOLE repo, tests included.** A `getattr(obj, "x", None)` "just to be safe" at the renderer seam trips the masquerade gate (baseline: 42 subjects, `config/cicd/masquerade_baseline.yaml`). String comparisons against `"failure"` / `"quarantined_at_source"` trip `manifest.symbol_inventory` (`rule.py:49-64`), which `.pre-commit-config.yaml` runs with `--root .`. | The whole branch, for every sibling. | Construct an owned dataclass at the renderer seam (ADR-032) and use direct attribute access. Write every outcome assertion against `TerminalOutcome.FAILURE` / `TerminalPath.QUARANTINED_AT_SOURCE`, never the literals. Free if done first; a whole-repo red if discovered at commit. |
| **Medium** | **`pip-audit --strict` is a REQUIRED check and its ignore list is hand-maintained.** `.github/workflows/ci.yaml:915-960` puts `supply-chain-audit` in `ci-success.needs`; `:750-757` carries exactly two justified `--ignore-vuln` entries. A future advisory against the renderer turns the merge gate red for the whole branch. | Every merge on the branch, indefinitely. | Make CVE cadence an explicit selection criterion with a named upstream security feed and a pin-bump cadence, recorded in the plan. Pre-agree the escape valve (pin-and-ignore with justification prose, matching the existing two). |
| **Medium** | **Neither automated security gate can see a native renderer CVE.** `pip-audit` inspects Python package advisories; CodeQL is `languages: python` only (`.github/codeql/codeql-config.yml`, `codeql.yaml:38`). A PDFium or poppler flaw is native C++. INFERRED (no network access to confirm advisory feeds). | The dependency review itself. | Do not let "pip-audit is green" stand as the review. Same mitigation as above. Related and also INFERRED: `pip-licenses --fail-on "GPL;AGPL"` (`ci.yaml:762-763`) would reject PyMuPDF/fitz outright, and is **blind** to a GPL system binary invoked as a subprocess — that branch needs a hand determination against `LABEL org.opencontainers.image.licenses="MIT"` (`Dockerfile:153`). |
| **Medium** | **A subprocess renderer is materially more expensive here than in a normal container and has no patch path.** `Dockerfile:144` is distroless (`gcr.io/distroless/python3-debian13`), there is no `apt-get` anywhere in it, and everything arrives via `COPY --from=builder /runtime-root/` (`:162`). Both architectures (`build-push.yaml:61`). Shared-library closure enumeration is INFERRED. | The image, the 7 CI apt sites, `tests/unit/deployment/test_aws_ecs_terraform_package.py`. | Weigh honestly: a spawn-context worker already delivers crash containment, wall-clock kill and a credential-free address space. A hand-maintained unpatchable native closure in a distroless image is a bad trade for the residual address-space gain. |
| **Medium** | **The `pyproject.toml` / `uv.lock` change collides with the worktree venv symlink.** All 7 CI jobs and both Docker stages use `uv sync --frozen`; a lock/manifest mismatch fails before any gate runs. AGENTS.md: worktrees symlink `.venv` to the main checkout. Also note the `all` extra is a **hand-flattened** list, so a new extra omitted from it silently breaks `uv pip install -e ".[all]"` while `--all-extras` CI stays green. | Everything. | Regenerate the lock in the MAIN checkout only; stage `pyproject.toml` + `uv.lock` together; verify `elspeth.__file__` before trusting any A/B measurement. |
| **Low** | **Trust-tier corpus cost is small but non-zero.** MEASURED 2026-08-21 by two lenses independently, agreeing: exit 1, **3161 finding lines** (R1 1044, R5 911, R_TB_SUPPRESSED 450, trust_tier.tier_model 278, R6 247, R4 90, R7 48, R8 34, R2 27, R9 26, allowlist.unused_rule 3, L1 2, R3 1). The closest sibling plugin contributes 3 findings. No `plugins/transforms/aws/*` per-file rule exists to shelter under. | The standing "never make it worse" obligation. | Capture before/after and diff the **per-rule table**, never the total. Write the renderer seam to avoid R1/R5/R6/R9 by construction (ADR-032: construct an owned type, then direct attribute access). If a suppression is genuinely needed, schedule the operator signing step — agents stage key-free. Caveat on comparability: the baseline run warned that `allow_hits[159]` binds to a deleted file and was refused; re-baseline if that is repaired. |
| **Low** | **Wardline stays green-relative-to-baseline under one checkable condition.** MEASURED 2026-08-21: exit 1, "736 files; 7027 findings; 6 active"; "129 recognized trust boundaries (fail_on_inert passed)". All 6 active ERRORs are PY-WL-102 in `web/composer/redaction.py` and `web/interpretation_state.py`; **zero in `plugins/`**. The pack recognises exactly two decorators (`scripts/wardline_pack.py:34-52`). | The gate of record. | Acceptance criterion: post-change wardline reports **6** active ERROR, not 7. Do not add a `@trust_boundary` to the renderer — which Correction 1 already forbids on tier grounds and which would also make it ERROR #7 without a raise-on-reject path. |
| **Low** | **Planner digest headroom is a ratchet, not a live risk.** MEASURED: full trained-operator digest 20,386 → **21,410** of 24,576 bytes with two new entries; largest existing transform entry is 525 B total; the trip point is ~8× house style. Overflow does not raise — it silently strips selection prose from **all** plugins (`planner_authoring_aids.py:1637-1643`). Demo-policy view measured 5,491 → 9,831 (untouched). | Trained-operator MCP composer only. | Keep new `description` + `usage_when_not_to_use` at house length. Record 21,410/24,576 in the plan so the next author inherits a number. |
| **Low** | **A new plugin declaring a `PluginCapability` hard-fails app startup on any deployment that renders `plugin_preferences`** (`web/plugin_policy/compiler.py` `incomplete_preference_order` → `ValueError` at `web/app.py:1220-1226`; Terraform renders preferences at `deploy/aws-ecs/terraform/modules/scenario/locals.tf:95-176`). Demo box is safe (both default empty). | AWS scenario deployments. | One-line design constraint: both new plugins declare **no** `policy_capabilities` (leave the `frozenset()` default, `base.py:1612`). This also keeps the tutorial's control-transform boundary (`tutorial_service.py:86-101`) untouched. |
| **Low** | **`_UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS` is FAIL-OPEN and an omission is silent** (`web/interpretation_state.py:165-167`, three members; the comment at `:160-164` says so). Whether the flag propagates *through* an aggregation node is **UNVERIFIED — inference**. | Downstream LLM untrusted-content reporting. | Make the call explicitly and write down the reasoning. Recommended: `pdf_rasterize` does NOT join (it emits pixels it rendered from bytes already in the row); the **stitcher** probably should, pending the propagation question in §8. |
| **Informational** | **Billing is attributable but uncosted and unreported, and retry evidence is purgeable.** `Call` (`contracts/audit.py:536-558`) has no cost/token/billable field; Textract is recorded as `CallType.HTTP` (`textract_client.py:751`) so `get_llm_usage_report` (`mcp/analyzers/reports.py:461-501`) cannot see it; the billable-attempt count lives in the response blob that retention deletes (`core/retention/purge.py:154-156,185`), while the `calls` row survives. Attribution itself works: expand children share the parent `row_id`. | Not changed by this work — but multiplied by it (1 call/document → N). | State it as a known accounting gap in the ticket. Cheap in-scope mitigation: have the stitcher record per-document arrived-page count and quarantine decision in its `success_reason` metadata, and add one assertion that a quarantined document's `calls` rows are reachable from its `row_id`. A cost column on `Call` is a separate ticket. |
| **Informational** | **`textract_inline_analysis.py:556`'s `Pages != 1` guard is NOT invalidated and must stay.** The exploder *satisfies* AWS's synchronous constraint, it does not relax it. | — | Say so in the ticket so nobody "fixes" it. Add the test it earns: a rasterized page whose response comes back `Pages: 2` must still error. |

### Where lenses contradicted each other

1. **`success_empty()` legality.** The gate resolution said legal; refuter #2 and three planning agents said abort. **Settled: abort** — I verified the call ordering directly (§2). Both claims are true about different code; the contract check runs first.
2. **`EXPECTED_TRANSFORM_COUNT` 33→34 vs 33→35.** The `pdf_rasterize` plan says 34 because it scoped only its own plugin. Full scope registers two new plugins, so **35** — and 51→53, 72→74 on the matrix legs.
3. **Stitcher naming (`batch_*` or not).** A `batch_*` name enrols it in `test_batch_catalogue_metadata.py:24-44,128-129`, which independently requires `aggregation.output_mode == "transform"` — coinciding exactly with the design's hard constraint. Two more table entries buys a gate. Not adjudicated; flagged in §8.

---

## 5. Blast radius, in rings

**(a) New code only.** `plugins/transforms/pdf_rasterize.py`; `plugins/infrastructure/rasterize/{__init__,protocol,worker}.py` (must live outside `PLUGIN_SCAN_CONFIG` and be spawned by absolute path, never `python -m` — the worktree venv symlink would resolve the worker from the main tree); the stitcher plugin; `contracts/expand_completeness.py`; `examples/expand_reassemble/`; new test modules.

**(b) Existing plugin surfaces.** `plugins/transforms/aws/textract_result.py` (third public entry point — **shared** with the async plugin, 901-line test file); `textract_inline_analysis.py` (hints `:727-732`, `usage_when_to_use` `:266-270`, `usage_when_not_to_use` `:271-275`, `source_file_hash` `:258`); `textract_document_analysis.py` (`:1086`, `:324-328`); `plugins/sources/blob_rows.py:7`; `contracts/errors.py` (at most one new `TransformErrorCategory`); `examples/textract_inline/README.md:3-4,11-13,44-46`.

**(c) Whole-tree gates and CI.** `tests/unit/plugins/test_discovery.py:265,300-304`; `test_catalog_reference_content.py:34,213-219,256`; `tests/unit/web/catalog/test_knob_schema_golden.py:39-40` + two new golden files; `tests/golden/state_engine/plugin_lifecycle_matrix.json` (six consumer modules, CI-checked at `ci.yaml:684`) + `docs/architecture/state_engine/proof-catalog/v3/catalog.json` + `scripts/state_engine_plugin_matrix.py:44,602`; `web/audit_readiness/boundary_expectations.py:148-186`; `tests/unit/contracts/test_plugin_assistance_coverage.py:41-56`; `tests/integration/web/test_catalog_discovery.py:52-99`; `test_composer_hint_config_consistency.py`; `test_core_catalogue_metadata.py` / `test_external_catalogue_metadata.py`; `test_validation_path_agreement.py:31`; `config/cicd/contracts-whitelist.yaml`; `tests/unit/docs/test_examples_readme_index.py:20`; masquerade + terminal-vocabulary lints (whole repo, tests included); mypy `strict` (a stubless renderer needs an overrides block with the documented verification bar); CodeQL. **Conditional on unit 1:** `config/cicd/runtime_rejection_parity.yaml` (any new `raise` or declarative pydantic constraint under `core/dag/` or `core/config.py`); `SQLITE_SCHEMA_EPOCH` + 4 doc files (only if a new `Index(...)` lands in `core/landscape/schema.py` — the Tier 1 read uses the **existing** index at `:613`, so it should not); `frontend/src/api/guidedDecoder.ts` (only if a composer-authorable contract field lands — invisible to every backend suite).

**(d) Deployment / dependency.** `pyproject.toml` base `dependencies` + `uv.lock`; the hand-flattened `all` extra; 7 CI `uv sync --frozen --all-extras` sites + 2 Dockerfile stages; `pip-audit --strict` (required check) and `pip-licenses --fail-on "GPL;AGPL"`; the distroless runtime (`Dockerfile:144,162-163`) and both build architectures; `/home/john/elspeth/.venv` shared with every worktree; the live `elspeth-web` systemd unit; ECS task memory envelopes; payload-store growth (N+1 objects per document — `max_pages × max_page_bytes` is the reviewed worst case, and retention scope grows with it).

**(e) Composer / demo.** Not exposed as shipped (§1c). Three conditional exposures, in order of sharpness:
1. **`aws_textract_document_analysis` is `OPERATOR_PROFILED`** (`textract_document_analysis.py:317`). In the profiled web deployment `web/plugin_policy/profiles.py:1188-1196` **replaces summary and composer_hints wholesale**. Fixing that plugin's own hints produces a green suite and a demo that still tells the planner the old story. The profile hint must be edited too — and must avoid the redaction substrings (`bucket`, `region`, `secret_ref`, …) pinned at `tests/unit/web/catalog/test_policy_view.py:161-186`.
2. **`web/composer/tools/sources.py:1033-1039`** embeds textract routing advice in the `set_source_from_blobs` **tool declaration** — the first thing the provider reads, and covered by **none** of the five hint gates. It went stale exactly as `:729` did and nothing will catch it next time.
3. **If a deployment allowlists these plugins for a demo**, the composer surfaces a plugin whose failure mode is a killed subprocess, and the fan-out guard (`web/execution/fanout_guard.py:405-421`) trips **two** markers for an explode→reassemble shape (advisory, not blocking).
**Explicit non-goal:** no new entry in `web/composer/recipes.py:947-1030` `_RECIPES`, no server-side PDF pipeline template, no plugin name in the static planner skills (guarded at `test_capability_skill_identity.py:155-179`). A server-authored graph would breach composer invariant #1; `composer_hints` do not, and no part of this work touches a composer tool handler, planner module, or tool-loop module.

---

## 6. The two forks, priced

| | **CHOSEN: rasterize + stitch** | **REJECTED FALLBACK: S3-stage in `aws_textract_document_analysis`** |
|---|---|---|
| Units of work | 3 (universal capability + exploder + stitcher) + hints + example | 1 (a staging path in an existing plugin) + hints |
| Engine work | **Yes** — Tier 1 fact + Tier 2 filter + a new commit arm on `AggregationExecutor` | **None** |
| **Expand-group completeness** | **Required** — this fork is the reason unit 1 exists | **Not required.** One document = one `StartDocumentAnalysis` call = one row in, one row out. No expansion, no expand-group, no stitcher, **no declaration trilemma, no all-quarantined abort.** Unit 1 evaporates on this fork. |
| PDF dependency | Native parser in **base** `dependencies` (`pyproject.toml:75-92`) on every install; a standing CVE-cadence obligation against a required `pip-audit --strict`; a licence determination the CI gate may be blind to; possibly a second native library (Pillow) for encoding | **None.** Zero new dependencies. |
| Container fidelity | **Yes** — pixels sidestep whatever container weirdness lost the image-borne tables | **No.** Does nothing for the originating incident's root symptom. |
| Multipage-local gap | Closed | Closed |
| New infrastructure | Payload-store amplification (N+1 objects/document); a rendering worker with rlimits | A staging bucket, a **WRITE** credential (the plugin is read-only against S3 today), a lifecycle policy, and a decision on whether staged bytes are audit-visible |
| Billing | N billable sync calls per document; no idempotency token, so retries re-bill | 1 async call per document |
| Whole-tree gate cost | 2 new plugins → 6 exact-set gates, 2 golden files, PB-09 adjudication | 1 existing plugin edited → config-surface + hint gates only |
| Demo risk | Subprocess kill surfaces in the composer; the fan-out guard trips two markers | Credential-egress-shaped config surface on an AWS-credentialed plugin |

**The honest comparison, now that the tier-model objection is withdrawn:** the S3 fork is roughly a
third of the work and carries **none** of the engine risk, because it never expands a row. That is a
material change to the comparison and it was not visible when the ticket was written. What it does
not buy is the thing the incident was actually about — container fidelity. The developer chose
container fidelity; this table is not an argument to reverse that, but the ticket's own reopening
trigger ("revisit if the dependency review goes badly") is arguably met by the base-`dependencies`
finding, and that is the developer's call to make with the price in front of them.

Note the two forks are not mutually exclusive: S3-staging closes the multipage-local gap cheaply and
immediately; rasterize+stitch is the container-fidelity answer and can follow.

---

## 7. The universal capability (Correction 2's centre of gravity)

**Where it should live.** In `AggregationExecutor` — the one shared component all 13 batch consumers
flow through — with the fact exposed read-only on `PluginContext`. Not in the stitcher, not in a
producer-stamping convention. A row-data convention requires nothing of the 13 and covers none of
them; it is per-pipeline discipline, not a capability. There is no engine-level grouping boundary to
hang it on otherwise: `group_by` appears **zero** times in `core/config.py` and is implemented five
separate times in two different algorithms across the plugins (linear-scan `same_scalar_bucket_value`
in `batch_top_k.py:217`, `batch_distribution_profile.py:286`, `batch_effect_size.py:242`,
`batch_experiment_compare.py:266`; dict `scalar_bucket_key` in `batch_stats.py:336`) — only 3 of 13
expose a `group_by` option at all.

**Who else it covers.** Tier 1 serves **all 13 automatically** with zero plugin code change and zero
behaviour change: the 12 `batch_*` transforms plus `report_assemble` (`report_assemble.py:99-104`).
Tier 2 serves, on config, any aggregation with `output_mode: transform` — 12 of 13
(`batch_replicate.py:135` declares `passes_through_input=True` and is rejected at **config** time, not
at runtime). Producers covered: `json_explode`, `line_explode`, `blob_csv_expand` and the future
`pdf_rasterize` all mint `expand_group_id` through the same `expand_token` path
(`engine/tokens.py:426-459`) — **no producer stamps anything**, which is the difference from the
row-data option.

**Are existing aggregations silently wrong today?** **Latent, not active — and the distinction
matters.** Every counter a batch plugin can see is arrival-derived (`accepted_count_total` at
`engine/executors/aggregation.py:285-287`, `rows_seen_total` at `:431`, `batch_size=len(rows)` at
`batch_effect_size.py:486` and `batch_experiment_compare.py:515`), so a consumer cannot distinguish
97-of-97 from 97-of-100. The five comparators — `batch_effect_size`, `batch_experiment_compare`,
`batch_drift_compare`, `batch_paired_preference`, `batch_classifier_metrics` — publish a *comparison*
over a possibly differentially-censored population; the sharpest case is
`batch_paired_preference.py:454`'s `incomplete_pair_count`, which already counts pairs missing an arm
but cannot say whether the arm never existed or failed upstream. The contrast proves the blindness is
structural: these plugins are meticulous about missing *field values inside arrived rows*
(`batch_classifier_metrics.py:279-292,449-470`). **But zero shipped example reaches it** — all 13
aggregation settings across `examples/batch_aggregation`, `examples/report_assemble`,
`examples/deaggregation` and `examples/statistical_batch_plugins` wire `input:` straight from the
source with an empty `transforms:` list, so no upstream transform can fail. Also unaffected: the
token-level audit trail is complete either way. This is an **output-row legibility** defect, not lost
evidence.

**Before, with, or after?** **Before.** Three reasons: (1) it discharges the blocker — without the
engine arm the primary use case aborts; (2) Tier 1 is complete-in-itself, changes no behaviour, and is
what the 14th consumer needs; (3) building it after the Textract work means the Textract work will
have implemented a substitute, which is precisely what Correction 2 forbids. **Splitting Tier 1 from
Tier 2 is a genuine option** — Tier 1 alone unblocks nothing but is cheap and safe; Tier 2 carries the
whole engine cost and is what the demo shape needs.

**Its sibling hole.** Unit 1 STEP 5's config validator (refuse `quarantine` unless `trigger` is
empty) and the missing builder guard (accept `trigger: {count: 3}` downstream of an exploder with no
error, while `builder.py:1344-1400` hard-fails the identical hazard downstream of `row_union`) are
**the same hole from two sides**. Close at least one before the shape is reachable from the composer.

---

## 8. Still unknown — with the cheapest settling experiment

| Unknown | Cheapest experiment |
|---|---|
| **Renderer choice** — the one item no agent will decide alone. Recommendation across two lenses converges: pypdfium2 (or equivalent) in an ELSPETH-owned spawn-context worker subprocess with rlimits. | Developer decision, not an experiment. But the base-`dependencies` consequence (§4) and the distroless consequence (§4) should be in front of them when they make it. |
| **Does the all-quarantined engine arm behave as designed?** The design rests on it and the two guards it interacts with have zero test references. | The Order-0 probe fixture. One file. |
| **Fork A vs B on the stitcher** — engine-computed filtering (plugin never sees incomplete groups) vs plugin-reported incompleteness. Drives unit 3 from MEDIUM to LARGE. | Decide it as part of unit 1's Tier 2 design; it is a design fork, not a measurement. |
| **Does the untrusted-content flag propagate through an aggregation node?** Decides whether the stitcher joins `_UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS` (`web/interpretation_state.py:165-167`). UNVERIFIED — inference from the comment that `_producer_reaches_untrusted` "falls through to its own upstream". | Read one function: `_producer_reaches_untrusted` in `web/interpretation_state.py`. |
| **Do composer tool declarations participate in any audit or cache hash?** Decides whether editing `sources.py:1033-1039` is hash-neutral. A grep for `tool_schema_hash`/`tools_hash` returned nothing, but the negative was not proven exhaustively. | Grep the composer audit module for the tool-declaration payload, or diff a session audit record before/after a one-character description change. |
| **Renderer licence facts.** PyMuPDF=AGPL and pypdfium2=Apache/BSD are both **INFERRED — not measured** (no network access in any agent session). | `pip download` + read the wheel METADATA, or check the project's LICENSE file offline once a candidate is chosen. Must be done before `pip-licenses` decides it for you. |
| **Whether the stitcher should be named `batch_*`.** A `batch_*` name enrols it in `test_batch_catalogue_metadata.py`, which independently pins `output_mode == "transform"` — a gate behind a constraint that otherwise has none. | Naming decision; the cost is two table entries. |
| **`--fail-on-inert` post-change wardline count.** Baseline 6 active ERROR / 129 boundaries measured. | Re-run the gate of record after the change and require exactly 6. |
| **Full-suite comparability.** `pytest tests/` takes ~18 min; sibling commits inside that window produced 456 phantom failures on 2026-08-17. | Record `git rev-parse HEAD` before and after; if HEAD moved, re-run rather than diagnose. |

---

## 9. Dissents

Three of five planning agents recorded no dissent on any DECIDED item. Two recorded notes, verbatim:

**`pdf_rasterize` — not a dissent, a clarification the DECIDED bullet does not cover:**

> "No dissent on any DECIDED item. One clarification that is not a dissent: the DECIDED bullet says
> the bound derives from BINARY_DOCUMENT_MAX_BYTES 'not a literal, because
> textract_inline_analysis.py:488 independently re-checks'. I have implemented that literally … and I
> add one thing the bullet does not: the DOWNSTREAM node's `max_document_bytes` may be configured
> LOWER than 5 MiB (that field permits reduction but never a raise, :162), in which case
> pdf_rasterize's `target_bytes` derived from the constant is too generous and every page is rejected
> at textract_inline_analysis.py:488. There is no cross-node validation for this. The composer hints
> must therefore tell the planner to keep `target_bytes` at or below the downstream
> `max_document_bytes`, and the DAG-level agreement should be an open ticket rather than a silent
> assumption."

**`worked_example` — a sequencing dissent, on the trigger guard:**

> "One note, on sequencing rather than on any DECIDED item. The plan above ships the example with
> `trigger: {}` protected only by a README comment, because I measured that the graph builder accepts
> a count trigger downstream of `json_explode` with no error while rejecting the structurally
> identical hazard downstream of a row_union (builder.py:1344-1400). I would prefer the expansion-group
> trigger guard to land BEFORE this example is indexed and before the shape is reachable from the
> composer, not as a follow-up ticket — an example that teaches a pattern whose only safety rail is
> prose is a worse artifact than no example, and the failure it invites (quarantining a document that
> never failed, with a clean audit trail) is exactly the kind an audit-first engine must not
> demonstrate. That said, the guard is universal work owned by the batch lane under Correction 2, not
> by this unit, so I have planned the example as if it stands on the README comment."

**`stitching_aggregation` — a scope note, not a disagreement:**

> "the brief scoped this unit as 'a new aggregation plugin', and the correct design makes it 'a new
> aggregation plugin PLUS a third public entry point in the shared normalizer'. That follows from the
> DECIDED north star (identical output shapes between the two Textract plugins) and from
> `_normalize_block_graph`'s own no-drift argument at textract_result.py:710-718 — it is not a
> widening of intent, but it does move the estimate and should not be a surprise later."

**Also recorded, from the phase-2 refuter, on the DECIDED items themselves:**

> "None on the DECIDED items. The failure semantics are right; it is the ENGINE that currently cannot
> express them at the aggregation point."
