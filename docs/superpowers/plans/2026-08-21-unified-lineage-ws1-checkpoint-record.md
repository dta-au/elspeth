# WS1 Checkpoint Record — unified-lineage (2026-08-23)

Evidence record for the WS1 checkpoint (WS1b plan Task 14, spec §11). Process
document under the normal delivery posture — no sign-off ceremony.

## Flip commit

- **`27414bbb0`** `feat!: retire tri-field lineage columns, TokenInfo.lineage_path is sole truth (WS1b flip)` — 126 files, +2539/−1485, schema epoch 34→35.
- Review fix rounds: `2253072c6` (D8 relief generalized to match `pop_fork_frame`; dict.get→indexing; CHANGELOG epoch; docstring; FrameKind literals), `5f9418307` (buried-FORK accept-path pin), `64b502e6d` (alias-caveat docstring), `71b5a8737` (ADR-032 getattr→nominal ast narrowing in the retirement guard).
- Task 13 guard: `54eb57bfd` + `7f5638d9b` (alias + functional-form hardening), mutation-checked (5 probes red-then-clean across implementer + independent reviewer reproduction).

## Step 1 — Full suite, HEAD-stable

- Final run at HEAD `71b5a8737`: **41607 passed, 0 failed**, 40 skipped, 1 xfailed; HEAD identical before/after (fence OK).
- Prior runs during Phase B: 41599/0 twice at `27414bbb0` (pre- and post-commit), 41601 + 1 isolated Hypothesis timing flake at `2253072c6` (test_redaction_completeness_property: 13/13 in isolation, zero diff overlap), 41606 + 1 at `64b502e6d` (the masquerade-gate finding fixed by `71b5a8737`).

## Step 2 — Frozen-oracle diff

- `pytest tests/integration/core/dag/test_oracle_freeze.py -q` (compare mode, `ELSPETH_ORACLE_FREEZE` unset): **50 passed, 0 failed**.
- FROZEN fixtures: **zero bytes changed** under `tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/` across the entire flip — verified by `git diff --stat` (empty) at review. This includes the 15 depth-1 scenarios of freeze commit `a74128d93`, `parallel-coalesces` (r23 casualty, frozen through WS1), and `fork-multiple-terminals-partial-failure` (pure fan-out, legal under §7 rule 2, permanently frozen).
- REGENERATED: `row-union-interleave` write-mode regeneration produced **byte-identical** output (md5 unchanged on both case files) — the ruling-27 popped-frame position is not observed by the frozen projection classes, so even the one authorized regeneration changed nothing.
- Golden JSONs: `git diff --stat tests/golden/` **empty** (no plugin schema changed in WS1).
- Manifest (`docs/architecture/dag/scenario-corpus/v1/manifest.yaml`) rotation, adjudicated structurally in the Phase B review (all 26 changed lines parsed old-vs-new as canonical JSON): every delta is (a) one inserted `group_record` entry in `audit_record_counts` (counts 1/3/5/6 per scenario), (b) for the four exact-kind cases the embedded export manifest `record_count` bumping by exactly that case's group_record count (109→112, 52→53, 56→57, 95→98), (c) one `resumed_full_projection_sha256` on the single summary-expectation case (its hash spans `audit_records`). **Zero stable-projection fields moved** — no `projection_sha256`, `projection_counts`, `rows`, `tokens`, `node_states`, `routes`, `terminal_dispositions`, `intermediate_outcomes`, `scheduler_work`, `batches`, or `expansions` deltas. No `group_loss` entries (stream empty until WS3, per D3). Two dated rotation-ledger notes (ruling-27 and export-surface) in `tests/unit/architecture/test_dag_scenario_corpus_contract.py`; `EXPECTED_CASE_REGISTRY_SHA256` rotated, `EXPECTED_EVIDENCE_REGISTRY_SHA256` untouched.

## Step 3 — Whole-tree gates

- Trust-tier (`elspeth-lints check --rules all --root src/elspeth`, shape-only mode): pre-Phase-A baseline **3168** → checkpoint **3164**. Delta: **−5 removed** (deleted tri-field replay/parse code in `core/landscape/data_flow/tokens.py`), **+1 added**: `config/cicd/enforce_tier_model/contracts.yaml` "Unused tier-model per-file rule: contracts/identity.py" — the rule is genuinely orphaned by the flip; the verified 7-line deletion is parked at `.superpowers/sdd/2026-08-21-unified-lineage-ws1b-flip-replay-checkpoint/finding-3-contracts-yaml.patch` because staging that file trips the whole-tree trust-tier hook, which currently fails to load on a pre-existing stale allowlist entry (`allow_hits[159]` → nonexistent `web/composer/guided/steps.py`) — judge-signed territory, operator-only ([O1]). **No source-code findings added.** The 4 transient dict.get additions and the getattr masquerade finding were fixed in-branch (`2253072c6`, `71b5a8737`).
- Wardline: `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` → exit 1 with **the same 6 pre-existing baseline ERRORs** (`web/interpretation_state.py` ×1, `web/composer/redaction.py` ×5); 129 recognized trust boundaries, fail-on-inert passed. **Delta zero.**
- Parity (`scripts/cicd/runtime_rejection_parity.py --write`): 289 sites, 0 unadjudicated, 0 dropped, yaml unchanged (verified at the Phase A boundary; no rejection-site changes in Phase B/C).

## §4.1a delta enumeration

- Pure-path truth table: `tests/unit/contracts/test_lineage_path_delta_table.py` (WS1b Task 2) — accessor-equivalence pinned case by case against `path_branch_name`/`path_fork_group_id`/`path_expand_group_id` innermost-frame semantics.
- Mint-integration pins re-pointed at accessors: `tests/unit/engine/test_token_lineage_path.py`, including the ruling-27 row-6 twin (released row_union token → all-None accessors).
- Resume-start arm order pinned: `tests/unit/engine/test_resume_start_dispatch.py` (merged/join → innermost-EXPAND → innermost-FORK → raise).

## Verdict

**CHECKPOINT GREEN.** Every observed delta is inside the enumerated §4.1a /
D3 / ruling-21 surfaces; frozen projections byte-identical; golden JSONs
untouched; trust-tier corpus net −4 with the single addition being the
operator-parked config finding; wardline delta zero. The STOP rule was not
tripped. WS2 may proceed.

## Known residue carried forward (not checkpoint failures)

- Operator items: `finding-3-contracts-yaml.patch` application after `allow_hits[159]` repair; judge-gated R8 fp churn (`fp=6cc704c857806df6`); stale judge bundles ≤2026-08-17 in `.elspeth/staged-reviews/`.
- Two Postgres testcontainer files (`test_token_outcome_atomicity_postgres.py`, `test_barrier_recovery_postgres.py`) collection-verified only — no Docker in this environment; needs one container CI pass before release.
- Pre-existing, orthogonal: `StableTerminalDisposition.error_hash` exclude_if vs stored-manifest explicit-null drift (will resurface at the next manifest rotation); `coalesce_tokens` strict-innermost anchor check (`tokens.py:528-529`) shares the D8 shape-sensitivity — adjudicate at WS2 entry.
- Bug `elspeth-258bd49d81` (expand fan-out width unfenced) — blocked on WS3 settlement; fix opportunistically there, else washup.
