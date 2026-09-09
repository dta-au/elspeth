# Per-file blanket migration — retire `per_file_rules` in favour of individually tracked exclusions (2026-08-30)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete all 85 `per_file_rules` blankets from `config/cicd/enforce_tier_model/*.yaml` so that every surviving tier-model suppression is either a semantically-scoped `@trust_boundary` in code or a judge-signed exact `allow_hits` entry — and the count of blanket rules is pinned at zero by the existing ratchet.

**Architecture:** The unit of work is a *blanket*, not a finding. Lanes (worktrees) dispose of every finding standing on their blankets by honest fix, `@trust_boundary`, or a per-site rationale sidecar; the hub merges a wave, deletes that wave's blankets from the operator-owned YAML in the merge commit (so the findings become *uncovered*), ratchets the ceilings honestly, stages one bundle per wave, annotates from the sidecars, and hands the operator a `sign-bundle` command. Three waves, ordered by contention (core/contracts/engine → plugins/edges → web).

**Tech Stack:** `elspeth_lints` (`trust_tier.tier_model`, `trust_boundary.*` gates, `check-per-file-blanket-ratchet`), `elspeth-judge` MCP staging (`stage_scan` / `stage_annotate` / `stage_preview` / `stage_status`), operator `elspeth-lints sign-bundle` (codex-cli transport, readonly tools), git worktrees.

**Spec:** This document is its own spec. The evidence it argues from is regenerable with `docs/plans/2026-08-30-per-file-blanket-migration-tools/blanket_census.py` (see Task 0). Prior-art: `docs/plans/2026-08-28-tier-model-justify-burndown.md` (lane contract, wave mechanics), `docs/agents/sweeps/tier-burndown/hub-ledger-2026-08-29.md`.

## Global Constraints

- **Starting state assumed:** bundle `sign-2026-08-30` (HEAD `c601c957b`, 1,063 actions) has been fired by the operator. If it has not, Task 0's census still runs, but Task 2's ceiling arithmetic must be redone from the post-sign tree.
- **Lanes never edit** `config/cicd/enforce_tier_model/*.yaml`, `.elspeth/`, or `elspeth-lints/`. The hub (one session) is the sole writer to the shared checkout and the only actor that deletes a blanket.
- **Agents never hold** `ELSPETH_JUDGE_METADATA_HMAC_KEY` ([O1]). Staging runs key-free; the operator fires. Never hand-edit a `judge_metadata_signature`.
- **Lints run keyless:** prefix every `elspeth-lints` invocation with `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing`.
- **Honest fix beats churn.** No aliasing, reordering, or dead code to move a fingerprint. No `match` rewrites of `isinstance` chains to dodge R5 — a lint dodge is not a fix. A site is *fixed* only when the flagged pattern is gone because the code no longer needs it.
- **Decorators are semantically aware; blankets are not.** `@trust_boundary` / `@observation_boundary` is allowed only where it is true: `tier=3`, the flagged subject is rooted at `source_param`, and the function actually parses external data. Every decorator carries `test_ref` + `test_fingerprint` (raising boundary) or `non_raising=True` (sentinel-returning boundary). Suppresses only R1/R5.
- **Every other surviving finding gets an individual entry**, i.e. a per-site rationale in `docs/agents/sweeps/tier-burndown/<bucket>.rationales.json` that the hub annotates onto the justify action. Rationale prose is first-class: it names the flagged pattern, why it is right *at this site*, and what change would invalidate it (fault class f0e38838d — a wrong mechanism defeats the judge mitigation).
- **Measurement is raw-corpus and per-blanket**, never a ceiling diagnostic: `blanket_census.py` before and after; a blanket is done when its `by_blanket` list is empty in the post-merge census.
- **Ratchet:** `check-per-file-blanket-ratchet` fails any push whose diff touches a source file still under a permanent multi-rule blanket. Therefore a wave's lane commits and the hub's blanket deletions land in **one push**. `max_per_file_rules` only goes down; `max_allow_hits` / `max_total_entries` go up by exactly the entries the wave adds, with the honest-accounting comment convention already used in `_defaults.yaml`.
- **Worktree hygiene:** `.claude/worktrees/tier-<bucket>`, `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src`, verify `elspeth.__file__` **and** `elspeth_lints.__file__` point into the worktree. `pytest -n 2` max per lane. Full suite once per wave, in a worktree, as a background job.

---

## Inventory (live tree @ `c601c957b`, `blanket_census.py`)

| | |
|---|---|
| Raw findings (allowlist disabled) | 2,094 |
| Blanket rules | 85 (72 capped, 13 uncapped) |
| Findings standing on blankets | **974** (surface the moment the blankets go) |
| — R1/R5 (decorator-eligible where Tier-3) | 918 |
| — R4/R6/R9 (fix or individual entry only) | 56 |
| Unused blankets (delete now, no other work) | 3: `contracts/identity.py` R5, `plugins/sinks/database_sink.py` R5, `web/composer/tools/_common.py` R1 |
| Files affected | 97 |
| Exact `allow_hits` entries today / ceiling | 601 / 652 (`max_allow_hits`); `max_total_entries` 745 |

The 974 never appeared in any earlier wave's worklist because worklists were built from *reported* findings and a blanket-covered finding is never reported. That is the coverage failure this plan closes.

### Disposition classes (lanes choose per site; the plan sets the expectation per file)

- **F — fix.** The pattern is wrong or unnecessary: env reads become membership-form (`if name in os.environ`), `dict.pop(k, None)` on an owned dict becomes a checked delete, an `except ValueError: return None` becomes an explicit error result, a `.get()` on an owned mapping becomes `[]`. Removes the finding from the raw corpus.
- **D — decorate.** Honest Tier-3 parse function; flagged subjects root at a parameter carrying external bytes/JSON/API response. `@trust_boundary(tier=3, source=..., source_param=..., suppresses=("R1","R5"), invariant=..., test_ref=..., test_fingerprint=...)` or `@observation_boundary(...)`. Removes the finding from the raw corpus (it becomes an `R_TB_SUPPRESSED` observation).
- **J — justify.** Policy-correct and not a Tier-3 boundary (Tier-1 `__post_init__` guards the allowed-context check misses, type dispatch in serialisers/walkers, protocol dispatch in the engine). Per-site rationale in the sidecar; becomes a judge-signed exact entry.

Expected shape per area: core/contracts/engine is overwhelmingly **J** (isinstance dispatch on owned or library types — the Jinja walker in `core/templates.py` alone is 142); plugins/telemetry/mcp/cli is mostly **D** with **F** for the 56 R4/R6/R9; web is small and mixed.

## Rulings (John, 2026-08-30) and one open item

1. **Renewal is the design.** Every exception is regularly re-approved by the judge; the 90-day expiry on a justify entry (`cli.py:2668`) is the mechanism, and the `resign` lane fired by the operator is how a renewal happens. A permanent (`expires: null`) suppression of any shape is therefore the defect this plan removes — the ~900 renewals that fall due ~2026-11-28 are the intended steady state, not a cost to avoid.
2. **Closed-set dispatch R5s go to the judge individually** (`core/templates.py` 142, `core/expression_parser.py` 54, `core/canonical.py` 23, `core/checkpoint/serialization.py` 20, `contracts/freeze.py` 15, `contracts/hashing.py` 10). No lint-precision carve-out; the readonly-harness judge is the semantic reviewer, per site, on every renewal.
3. **Open — `_R5_NAMED_BOUNDARY_CONTEXTS`** (`rule.py:510`): an in-lint, per-function, unsigned and never-expiring allow table (~25 functions). Under ruling 1 it is the same defect class as a blanket (permanent, not judge-tracked). Out of this plan's scope; John to say whether it becomes a Wave 4.

---

## Task 0: Post-sign census and bucket manifest (hub)

**Files:**
- Create (done, dry-run verified): `docs/plans/2026-08-30-per-file-blanket-migration-tools/blanket_census.py`
- Create: `docs/plans/2026-08-30-per-file-blanket-migration.buckets.json`

**Interfaces:**
- Produces: `census.json` with `by_blanket` (blanket id → `file:line:RULE` list), `by_file` (file → rule → count), `unused_blankets`. Blanket id shape: `<yaml>::<pattern>::<R,R>::max_hits=<n|None>`.

- [ ] **Step 1: Confirm the operator fired `sign-2026-08-30`** — `git log --oneline -3 -- config/cicd/enforce_tier_model/` shows the signing commit, and `mcp__elspeth-judge__verify_signatures` reports no unsigned judge-gated entries. If not fired, stop and tell John; do not proceed on the stale assumption.

- [ ] **Step 2: Run the census on the signed tree**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
PYTHONPATH=elspeth-lints/src .venv/bin/python \
  docs/plans/2026-08-30-per-file-blanket-migration-tools/blanket_census.py \
  > "$SCRATCH/census-post-sign.json"
```
Expected on stderr: `raw=… blankets=85 standing=… unused=3` (standing should be ≤ 974: signing may have converted some blanket-covered findings that also had justify actions).

- [ ] **Step 3: Write `buckets.json`** from `by_file` using the bucket table below (LOC from `wc -l src/elspeth/<file>`; a file is never split; ≤5,000 LOC per lane except the two solo files). Manifest shape identical to `2026-08-28-tier-model-justify-burndown.buckets.json` (`id, wave, model, loc, findings, files[{file, loc, findings, rules{}}]`).

- [ ] **Step 4: Reconcile** — `sum(findings over buckets) == standing_on_blankets` and every file in `by_file` appears in exactly one bucket. Print both numbers. A mismatch is a plan defect; fix the table, not the number.

- [ ] **Step 5: Commit the manifest** (hub, pathspec only)

```bash
git add docs/plans/2026-08-30-per-file-blanket-migration.md \
        docs/plans/2026-08-30-per-file-blanket-migration.buckets.json \
        docs/plans/2026-08-30-per-file-blanket-migration-tools/blanket_census.py
git commit -m "docs(plans): per-file blanket migration — census tool + bucket manifest"
```

---

## Task 1: Delete the three unused blankets (hub)

**Files:**
- Modify: `config/cicd/enforce_tier_model/contracts.yaml` (rule `contracts/identity.py` R5 max_hits 1)
- Modify: `config/cicd/enforce_tier_model/plugins.yaml` (rule `plugins/sinks/database_sink.py` R5 max_hits 1)
- Modify: `config/cicd/enforce_tier_model/web.yaml` (rule `web/composer/tools/_common.py` R1 max_hits 10)
- Modify: `config/cicd/enforce_tier_model/_defaults.yaml:248` `max_per_file_rules: 99 → 82` (85 − 3)

- [ ] **Step 1: Prove each is unused on the live tree** — `census-post-sign.json` `unused_blankets` lists exactly these three; and `elspeth-lints check --rules trust_tier.tier_model --root src/elspeth` prints `Unused tier-model per-file rule: <pattern>` for each.

- [ ] **Step 2: Delete the three `- pattern:` blocks** (whole block: pattern, rules, reason, expires, max_hits).

- [ ] **Step 3: Ratchet down** — in `_defaults.yaml` set `max_per_file_rules: 82` with a dated comment: `# 2026-08-30: 99 → 82 — deleted the 3 per_file_rules that covered zero findings (identity.py R5, database_sink.py R5, _common.py R1); no findings surfaced (blanket_census.py unused_blankets).`

- [ ] **Step 4: Verify nothing surfaced**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules trust_tier.tier_model --root src/elspeth 2>&1 \
  | grep -cE '^[a-z0-9_/]+\.py:[0-9]+:'
```
Expected: identical to the same count taken before Step 2 (COUNT, never `tail`), and the three `Unused` lines gone.

- [ ] **Step 5: Ratchet check passes**

```bash
PYTHONPATH=elspeth-lints/src .venv/bin/python -m elspeth_lints.core.cli \
  check-per-file-blanket-ratchet --baseline-ref HEAD --allowlist-root config/cicd --repo-root .
```
Expected: exit 0, `head_blanket_count` 82 (deletion is monotonic cleanup).

- [ ] **Step 6: Commit** — `git add config/cicd/enforce_tier_model/{contracts,plugins,web,_defaults}.yaml && git commit -m "chore(tier-model): delete 3 unused per_file_rules; max_per_file_rules 99→82"`

---

## Task 2: Wave-merge procedure (hub; repeated once per wave — this is the task the lane tasks feed)

**Files:**
- Modify: `config/cicd/enforce_tier_model/<area>.yaml` — delete the wave's blankets
- Modify: `config/cicd/enforce_tier_model/_defaults.yaml` — ceilings
- Modify: `docs/agents/sweeps/tier-burndown/hub-ledger-2026-08-29.md` — wave entry
- Read: `docs/agents/sweeps/tier-burndown/<bucket>.rationales.json` (lane deliverables)

**Interfaces:**
- Consumes: each lane's commit(s) on branch `tier-<bucket>` in its worktree, plus sidecar `{ "<file>:<RULE>:<symbol>:ast=<path>": "<rationale>" }`.
- Produces: staged bundle `.elspeth/staged-reviews/blanket-w<N>-<date>.json`, operator command.

- [ ] **Step 1: Merge every lane of the wave** with `--no-ff` into `feature/unified-lineage`; resolve conflicts in favour of the lane's disposition; do not push yet.

- [ ] **Step 2: Delete the wave's blankets** from the area YAML — every `- pattern:` block listed in the wave's table. Do not narrow, do not lower `max_hits`; delete.

- [ ] **Step 3: Re-census** (`blanket_census.py` → `census-w<N>-post.json`). Expected: every deleted blanket id absent from `by_blanket`; `standing_on_blankets` down by exactly the wave's total. Any finding a lane neither fixed, decorated, nor rationalised now shows as *uncovered* in `elspeth-lints check` — list them; they are lane defects and go back to the lane before Step 4.

- [ ] **Step 4: Ratchet the ceilings honestly** in `_defaults.yaml`: `max_per_file_rules` −(blankets deleted); `max_allow_hits` and `max_total_entries` +(J entries the wave will add = number of sidecar keys that survive Step 3). Dated comment naming the wave, the counts, and this plan.

- [ ] **Step 5: Gates on the merged tree** (worktree, background): full `pytest tests/`; `elspeth-lints check --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth` (CI-only gate — every new decorator's `test_fingerprint` is verified here and nowhere else); `check-per-file-blanket-ratchet --baseline-ref <pre-wave HEAD>` exit 0.

- [ ] **Step 6: Commit + push once** — lanes' merges and the YAML/ceiling commit in the same push, so the ratchet sees the touched files with their blankets already gone.

- [ ] **Step 7: Stage one bundle** (key-free shell — `stage_scan` fails closed if the HMAC key is in the environment): `mcp__elspeth-judge__stage_scan` → bundle id; `stage_annotate` with the map produced by `docs/plans/2026-08-30-per-file-blanket-migration-tools/sidecar_join.py <wave sidecars>` run on the MERGED tree (join axis is `(file, rule, symbol_context, ast_path)` only; the `fp=` is re-derived live because fingerprints hash the positional ast path and a lane's cached `fp=` is valid only at its own tip; the `(file, rule, symbol)` triple is non-unique in every Wave-1 bucket and must never be used; the tool exits 1 on any unbound key — send those back to the lane); `stage_preview` to surface BLOCKs; `stage_status` for the paste-ready command. Expected: `new_judgment` action count == number of uncovered findings after Step 3; un-annotated count 0.

- [ ] **Step 8: Hand the operator the command** from `stage_status` (`sign-bundle --lanes new_judgment --judge-transport codex-cli --judge-tools readonly …`, `--continue-on-block` when >10 calls) and the ledger entry. The tree must stay at the staged HEAD until fired; any commit re-stages (one `stage_scan` + re-annotate).

- [ ] **Step 9: After the fire** — re-census; expected `standing_on_blankets` unchanged (the deleted blankets are gone) and the real-allowlist finding count for the wave's files == 0. Record in the ledger. BLOCKs come back as a remediation list for the next wave's lanes.

---

## Lane contract (every bucket task below inherits this verbatim)

Deliverable = commit(s) on the lane branch + sidecar + a Filigree comment on the bucket issue listing `fixed`, `decorated`, `justified` keys with counts that sum to the bucket's findings. A lane is not done until its blanket's `by_blanket` list would be empty.

- [ ] Worktree: `git worktree add .claude/worktrees/tier-<bucket> -b tier-<bucket> feature/unified-lineage`; `ln -s "$(git rev-parse --show-toplevel)/.venv" .venv`; export `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src`; verify both `__file__`s.
- [ ] Read first: `docs/agents/recent-code-hints.md` (esp. 2026-08-29 entries on `@trust_boundary` fingerprints and per-file ceilings), ADR-032, `src/elspeth/contracts/trust_boundary.py` docstring, `CONTRIBUTING.md § Whole-tree gates`.
- [ ] Baseline: `blanket_census.py` `by_blanket` list for your blankets (the hub pastes it into the brief). Work every site in it.
- [ ] Per site, in preference order: **F** (only if the code is genuinely wrong or the pattern unnecessary) → **D** (only if honestly Tier-3, param-rooted) → **J** (rationale). Never blanket, alias, or reorder.
- [ ] Every new decorator: raising boundary gets a real pytest `test_ref` whose body invokes the function through `source_param` with malformed input and asserts the raise; `test_fingerprint` computed via `elspeth_lints.rules.trust_boundary.tests.rule._resolve_test_ref(test_ref, repo_root)` — it returns `_ResolvedTestRef` (read `.fingerprint`) or `_TestRefResolutionError` (your nodeid is wrong; fix it, do not guess) — never hand-typed. Non-raising boundaries use `@observation_boundary`. Then run `elspeth-lints check --rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier --root src/elspeth` — a green `pytest` proves nothing about this gate.
- [ ] Sidecar `docs/agents/sweeps/tier-burndown/<bucket>.rationales.json`: keys `file:RULE:symbol:ast=<path>` exactly as `elspeth-lints check` prints them; value = the rationale (pattern, why right here, what would invalidate it).
- [ ] Evidence: raw corpus count before/after (`--allowlist-dir` copy with `per_file_rules: []`, findings-only regex `^[a-z0-9_/]+\.py:[0-9]+:`), scoped tests green (`pytest -n 2 tests/unit/<area>`), whole-tree AST gates untouched (no new `getattr`, no masquerade sites).
- [ ] Commit on the lane branch; do NOT commit to the shared checkout; message the hub with the branch name and the three counts.

---

## Wave 1 — core / contracts / engine / tui / testing (602 findings, ~40k LOC; mostly J)

Blankets deleted at wave merge: every `per_file_rules` block in `core.yaml`, `contracts.yaml`, `engine.yaml`, `tui.yaml`, `testing.yaml` (14 + 17 + 10 + 3 + 1 = 45 after Task 1's identity.py deletion).

| Bucket | Model | LOC | Findings | Files (findings by rule) | Expected disposition |
|---|---|---:|---:|---|---|
| C01 | opus | 1921 | 142 | `core/templates.py` (R5×142) | J — Jinja2 `nodes.*` visitor dispatch; one rationale *per site* naming the node class handled and why a wrong node must not be silently skipped. Any site that silently falls through on an unexpected node is **F** (raise). |
| C02 | opus | 1980 | 107 | `core/expression_parser.py` (R5×54) · `core/canonical.py` (R5×23) · `core/checkpoint/serialization.py` (R5×20) · `contracts/hashing.py` (R5×10) | J — `ast` node dispatch and numpy/pandas/NaN type envelopes. NaN/Infinity rejection guards are Tier-1 integrity and read as **J** with the crash path named. |
| C03 | **fable** | 4412 | 86 | `core/config.py` (R5×52, R1×13, R6×2) · `core/secrets.py` (R5×16) · `core/security/config_secrets.py` (R1×2) · `core/security/secret_loader.py` (R1×1) | Mixed. `config.py` YAML boundary parse → **D** on the actual `from_dict`/validator entry points (check `_is_pydantic_before_validator` already exempts before-validators; the 52 remaining are not those). `os.environ.get` R1 → **F** membership-form or **D**. `except ValueError` R6×2 → **F** or J with the error result named. `secrets.py` tree-walk → J. |
| C04 | opus | 3434 | 111 | `contracts/runtime_val_manifest.py` (R5×53, R6×4) · `contracts/freeze.py` (R5×15) · `contracts/type_normalization.py` (R5×14) · `contracts/contract_records.py` (R5×12) · `contracts/call_data.py` (R5×9) · `contracts/events.py` (R5×4) | J — deep-freeze / serialisation dispatch. The four `except AttributeError/TypeError` in `runtime_val_manifest.py` are **F** unless the swallowed exception is reified into the manifest hash input (then J, saying so). |
| C05 | opus | 3509 | 72 | `contracts/schema.py` (R5×17, R1×9) · `contracts/config/runtime.py` (R5×14) · `contracts/plugin_context.py` (R5×15) · `contracts/token_usage.py` (R5×11) · `contracts/results.py` (R5×6) | `schema.py from_dict` and `token_usage.from_dict` → **D** (external YAML / LLM API response, param-rooted). `plugin_context` external-response validation → **D**. `results.py` PipelineRow output guards → J (Tier-1). |
| C06 | opus | 4500 | 35 | `contracts/audit.py` (R5×4) · `contracts/secret_scrub.py` (R5×4) · `contracts/header_modes.py` (R5×1) · `contracts/diversion.py` (R5×1) · `tui/screens/explain_screen.py` (R5×11) · `tui/widgets/node_detail.py` (R5×5) · `tui/widgets/lineage_tree.py` (R5×2) · `testing/__init__.py` (R5×7) | J — `__post_init__` guards the allowed-context check misses (rationale states why the check misses them), TUI projection guards over audit rows. `testing/__init__.py` dict|PipelineRow convenience → **F** if callers can pass one type; else J. |
| E01 | opus | 5505 | 7 | `engine/processor.py` (R5×7) | J — Protocol dispatch; solo because of size and contention. |
| E02 | opus | 4771 | 17 | `engine/token_traversal.py` (R5×4) · `engine/triggers.py` (R5×3) · `engine/scheduler_drain.py` (R5×3) · `engine/executors/gate.py` (R5×3) · `engine/executors/transform.py` (R5×2) · `engine/dag_navigator.py` (R5×2) | J — union narrowing on owned result types; sites that narrow a `str` route label from the expression parser are Tier-3-adjacent but not param-rooted → J. |
| E03 | opus | 3769 | 5 | `engine/barrier_coordination.py` (R5×1) · `engine/tokens.py` (R5×1) · `engine/batch_adapter.py` (R5×1) · `core/landscape/formatters.py` (R5×2) | J. |
| L01 | opus | 3117 | 13 | `core/landscape/database.py` (R5×7) · `core/landscape/journal.py` (R5×5) · `core/operations.py` (R5×1) | `urllib.parse` union narrowing and SQL param normalisation → J; `IntegrityError` discrimination → J with the crash-vs-retry contract named. |
| L02 | opus | 3488 | 7 | `core/landscape/run_lifecycle_repository.py` (R5×5) · `core/dag/graph.py` (R5×2) | J — Tier-1 integrity validation on deserialised audit rows. |

Wave-1 merge: Task 2 with N=1. Expected ceiling deltas: `max_per_file_rules` 82 → 37; `max_allow_hits` +(J count, ≈ 550–600).

## Wave 2 — plugins / telemetry / mcp / composer_mcp / cli (333 findings, ~31k LOC; mostly D + the 56 F)

Blankets deleted at wave merge: every block in `plugins.yaml` (20 after Task 1), `telemetry.yaml` (5), `mcp.yaml` (3), `composer_mcp.yaml` (1), `cli.yaml` (3) = 32.

| Bucket | Model | LOC | Findings | Files (findings by rule) | Expected disposition |
|---|---|---:|---:|---|---|
| P01 | opus | 4209 | 34 | `cli.py` (R1×11, R5×8, R4×8, R6×6) · `cli_helpers.py` (R1×1) | R1 env reads → **F** membership-form. R4×8 broad-except in CLI top-level → **F** to a typed error result, or J naming the exit-code contract each preserves. R6×6 (`OSError`, `URLError`, `FileExistsError`) → **F** unless the swallow is reified into a user-facing message (then J). R5 plugin dispatch / SQLite URI → J. |
| P02 | opus | 3530 | 34 | `mcp/server.py` (R5×14, R1×9) · `mcp/analyzers/queries.py` (R1×7) · `mcp/analyzers/diagnostics.py` (R1×3) · `mcp/analyzers/reports.py` (R1×1) | **D** — MCP tool arguments are external and param-rooted; one decorator per tool handler / analyzer entry, raising `ToolArgumentError`-class with a test. Whatever is not param-rooted → J. |
| P03 | opus | 3420 | 34 | `composer_mcp/server.py` (R5×14) · `plugins/infrastructure/config_base.py` (R5×7) · `plugins/infrastructure/schema_factory.py` (R5×5) · `plugins/infrastructure/clients/json_utils.py` (R5×3) · `plugins/infrastructure/clients/llm.py` (R5×3) · `plugins/infrastructure/clients/replayer.py` (R5×1) · `plugins/infrastructure/utils.py` (R5×1) | `composer_mcp` serialisation of owned Pydantic models → J. `config_base` field coercion of Tier-3 YAML → **D** on the coercion entry. `json_utils` NaN detection → J (Tier-1 integrity). |
| P04 | **fable** | 4371 | 48 | `plugins/transforms/llm/transform.py` (R5×19) · `llm/validation.py` (R5×12) · `llm/providers/bedrock.py` (R5×6) · `llm/multi_query.py` (R5×3) · `llm/image_inputs.py` (R5×3) · `llm/templates.py` (R5×2) · `llm/providers/azure.py` (R5×1) · `llm/provider.py` (R5×1) · `llm/langfuse.py` (R5×1) | **D** — LLM response validation is the canonical Tier-3 parse; decorate the response parsers, not the transform's `process`. Provider dispatch on owned provider classes → J. |
| P05 | **fable** | 1904 | 33 | `plugins/transforms/azure/document_intelligence.py` (R5×11, R1×6, R9×2, R6×1) · `azure/document_intelligence_result.py` (R5×2, R1×2, R6×1) · `azure/content_safety.py` (R1×3, R5×1) · `azure/base.py` (R9×2, R5×2) | **D** on the API-response parsers. R9 `self._http_clients.pop(state_id, None)` / `self.__dict__.pop(...)` on owned state → **F** (checked delete). R6 `except (TypeError, ValueError)` → **F** or J with the quarantine result named. |
| P06 | opus | 2297 | 71 | `telemetry/exporters/otlp.py` (R1×10, R5×9, R6×1) · `exporters/datadog.py` (R5×12, R1×4, R9×2) · `exporters/azure_monitor.py` (R5×9, R1×3) · `exporters/console.py` (R5×9, R1×2) · `telemetry/serialization.py` (R5×8, R6×1) · `telemetry/factory.py` (R5×1) | R1 env/config reads → **F** membership-form. R9 `os.environ.pop(..., None)` → **F**. R5 attribute serialisation dispatch → J. `except ImportError` optional-dependency guard → J (state the fallback). |
| P07 | **fable** | 3441 | 18 | `plugins/sources/aws_s3_source.py` (R5×14) · `sources/azure_blob_source.py` (R5×3) · `sources/csv_source.py` (R5×1) | **D** — schema inference over external rows, param-rooted on the row/object. |
| P08 | **fable** | 4443 | 48 | `plugins/sources/llm/source.py` (R5×14) · `sources/json_source.py` (R5×4) · `sources/dataverse.py` (R5×4) · `plugins/sinks/dataverse.py` (R5×6) · `infrastructure/clients/dataverse.py` (R5×7, R1×5, R6×1) · `infrastructure/clients/fingerprinting.py` (R6×4, R1×3) | **D** on OData / LLM response parsers. `fingerprinting.py` R6 `except ValueError` ×4 → **F** (a fingerprint that fails to parse must not silently degrade) unless the contract is documented degrade → J. |
| P09 | opus | 3514 | 13 | `plugins/sinks/json_sink.py` (R5×1) · `sinks/chroma_sink.py` (R4×1) · `transforms/safety_utils.py` (R5×4) · `transforms/type_coerce.py` (R1×2) · `transforms/keyword_filter.py` (R5×2) · `transforms/batch_stats.py` (R5×2) · `transforms/json_explode.py` (R5×1) | Contract-enforcement `isinstance` (array must be list, non-string rejection) → J (Tier-2 crash path). `chroma_sink` R4 post-audit telemetry → **F** to the telemetry error-entry idiom (`_records_error_entry_in_accumulator`) or J. `type_coerce` optional mapping `.get` → **F** membership-form. |

Wave-2 merge: Task 2 with N=2. Expected `max_per_file_rules` 37 → 5.

## Wave 3 — web (39 findings; John's composer work is live here — run it when no composer lane is open)

Blankets deleted at wave merge: every block in `web.yaml` (5 after Task 1) = 5 → `max_per_file_rules: 0`.

| Bucket | Model | LOC | Findings | Files (findings by rule) | Expected disposition |
|---|---|---:|---:|---|---|
| W01 | opus | 4078 | 13 | `web/composer/guided/chat_solver.py` (R5×13) | **D** — LLM-output parsing in the Step-1/Step-2 solvers, param-rooted on the model reply. |
| W02 | opus | 3975 | 16 | `web/composer/tools/_common.py` (R5×6) · `web/composer/telemetry_phase8.py` (R4×10) | `_common.py` → **D** (precedent B40, `@observation_boundary`). `telemetry_phase8` R4×10 — the W5 telemetry-only exemption is one rationale; write it **per site** (each `record_*`) so it becomes 10 signed entries, or **F** to the error-entry idiom. |
| W03 | opus | 11894 | 10 | `web/composer/tool_batch.py` (R6×5) · `web/composer/service.py` (R6×5) | J — LLM tool-argument JSON parse failures reified as `ToolArgumentError` / audit-write-then-raise; each rationale names the error result the handler returns. Read only the sites; do not refactor these files. |

Wave-3 merge: Task 2 with N=3, and additionally:

- [ ] `_defaults.yaml`: `max_per_file_rules: 0`, `max_permanent_per_file_rules: 0`; comment names this plan.
- [ ] Final census: `blanket_rules == 0`, `standing_on_blankets == 0`. Paste both lines into the ledger.
- [ ] `check-per-file-blanket-ratchet --baseline-ref <wave-3 pre-merge HEAD>` exit 0 with `head_blanket_count == 0`.

---

## Task 3: Pin the end state (hub, after Wave 3 is signed)

**Files:**
- Create: `tests/unit/elspeth_lints/test_per_file_blanket_retired.py` (no existing lints test loads the live allowlist purely for its budget; `test_allowlist_budget.py` is all tmp-path fixtures)
- Modify: `AGENTS.md` § Judge-signature stage — one sentence: per-file blankets are retired; new suppressions are decorators or judge-signed entries only.

- [ ] **Step 1: Write the failing test**

```python
"""The live tier-model allowlist carries no per-file blankets.

Retired by docs/plans/2026-08-30-per-file-blanket-migration.md. A new
``per_file_rules`` entry is a regression, not a configuration choice: every
suppression is a ``@trust_boundary`` in code or a judge-signed ``allow_hits``
entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth_lints.rules.trust_tier.tier_model.rule import _load_tier_model_allowlist

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_live_tier_model_allowlist_has_no_per_file_blankets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keyless load, same downgrade the agent-side gate runs under ([O1]).
    monkeypatch.delenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", raising=False)
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE", "shape-only-when-key-missing")

    allowlist = _load_tier_model_allowlist(
        _REPO_ROOT / "config" / "cicd" / "enforce_tier_model",
        source_root=_REPO_ROOT / "src" / "elspeth",
    )

    assert allowlist.per_file_rules == []
    assert allowlist.max_per_file_rules == 0
```

- [ ] **Step 2: Run it** — `pytest tests/unit/elspeth_lints/test_per_file_blanket_retired.py -v`. Expected: FAIL while any blanket remains (`per_file_rules` non-empty); PASS after Wave 3's YAML lands.

- [ ] **Step 3: AGENTS.md sentence** under "Judge-signature stage": `Per-file blankets (per_file_rules) are retired (2026-08); a suppression is a @trust_boundary on an honest Tier-3 parse site or a judge-signed allow_hits entry — never a file-level ceiling.`

- [ ] **Step 4: Commit** — `git add tests/unit/elspeth_lints/test_allowlist_budget.py AGENTS.md && git commit -m "test(tier-model): pin zero per_file_rules; AGENTS.md notes blanket retirement"`

---

## Cost and sequencing summary

| Wave | Lanes | Findings | Blankets deleted | Judge calls (upper bound = J count) | Operator fires |
|---|---:|---:|---:|---:|---:|
| Task 1 | 0 | 0 | 3 | 0 | 0 |
| 1 | 11 | 602 | 45 | ≤ 602 (expect ≈ 550) | 1 |
| 2 | 9 | 333 | 32 | ≤ 333 (expect ≈ 120 — most go D/F) | 1 |
| 3 | 3 | 39 | 5 | ≤ 39 | 1 |

At ~18.5 s per codex-cli readonly call, Wave 1's fire is ≈ 3 hours of judge time; run it overnight with `--continue-on-block`. Concurrency: ≤ 8 worktree lanes at once (`-n 2` each); nothing else touches `web/composer` while Wave 3 runs.

## Self-review

- Coverage: every one of the 85 blankets is named in Task 1 (3) or a wave table's deletion list (45 + 32 + 5 = 82); every one of the 97 files with standing findings appears in exactly one bucket (reconciled in Task 0 Step 4 against the regenerated census, not this table).
- No placeholders: dispositions are stated per file; the mechanism for each class is concrete (decorator kwargs, sidecar key shape, ceiling edits, commands).
- Type consistency: `blanket_census.py` output keys (`by_blanket`, `by_file`, `unused_blankets`, `standing_on_blankets`, `blanket_rules`) are the names Tasks 0–3 read.
- Known gap the plan does not close (listed under Rulings, item 3): `_R5_NAMED_BOUNDARY_CONTEXTS`.
