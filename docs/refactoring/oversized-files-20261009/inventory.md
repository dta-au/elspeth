# Tracked-file physical-line inventory

Measured on Nyx. Fixed Git commits are authoritative; the separate tracked repair overlay is a best-effort sample of a moving worktree. No source, test, lock, fixture, policy, or data file was edited for this inventory.

## Counting rule and scope

`count_tracked_lines.py` enumerates `git ls-tree -rz --full-tree <commit>` and reads raw objects through `git cat-file --batch`. Every tree entry is represented, including excluded binaries, symlinks and gitlinks. Only regular modes `100644` and `100755`, strict UTF-8, non-NUL blobs receive a line count. Physical lines = LF bytes + one for a nonempty final segment without LF. CRLF is one line; CR-only and Unicode line separators do not split lines. Empty blobs are zero lines. Thresholds are strictly `>5000` and `>10000`; 5,000 and 10,000 themselves are excluded.

Untracked and ignored files, Git content filters, installed dependencies, and filesystem glob/regex discovery are outside the fixed inventory. Paths are repository-relative. The default category rules are descriptive heuristics for smaller entries; every oversized entry below was manually reviewed for executable code versus artifact provenance.

## Exact snapshots

| Label | Commit | Entries | Counted text | Excluded | >5000 | >10000 | Authored >5000 | Authored >10000 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| main | `9e481bc85a32ee15f808c5e2b16cab7634e57b8e` | 6133 | 6109 | 24 | 36 | 13 | 25 | 9 |
| published-pr281 | `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc` | 6393 | 6369 | 24 | 38 | 13 | 26 | 9 |
| streaming-local | `d2b73990d137e9725200c897544eebc5b958fdf4` | 6135 | 6111 | 24 | 36 | 13 | 25 | 9 |
| repair-integrated | `703cdd1674d01d422baa4d54f210105edccf498c` | 6395 | 6371 | 24 | 38 | 13 | 26 | 9 |
| repair-live-overlay | `703cdd1674d01d422baa4d54f210105edccf498c` | 6395 | 6371 | 24 | 38 | 13 | 26 | 9 |

Raw output (complete file and per-blob identities: [raw-measurements.txt](raw-measurements.txt)):

```text
Measured UTC: 2026-10-08T15:42:36.714386+00:00
snapshot=main commit=9e481bc85a32ee15f808c5e2b16cab7634e57b8e entries=6133 text=6109 excluded=24 gt5000=36 gt10000=13
snapshot=published-pr281 commit=9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc entries=6393 text=6369 excluded=24 gt5000=38 gt10000=13
snapshot=streaming-local commit=d2b73990d137e9725200c897544eebc5b958fdf4 entries=6135 text=6111 excluded=24 gt5000=36 gt10000=13
snapshot=repair-integrated commit=703cdd1674d01d422baa4d54f210105edccf498c entries=6395 text=6371 excluded=24 gt5000=38 gt10000=13
snapshot=repair-live-overlay commit=703cdd1674d01d422baa4d54f210105edccf498c entries=6395 text=6371 excluded=24 gt5000=38 gt10000=13
changed_rows=1726 oversized_overlap_rows=39
```

## Exhaustive oversized union

M = main; P = published PR281; S = streaming local commit; R = repair integrated commit; O = observed repair tracked overlay. The 38-path union includes all categories. The compact CSV records bytes, mode, blob ID, status and category per entry; a changed row records the complete new entry, so sizes are recoverable for every snapshot.

| Repository-relative path | Category | M lines | P lines | S lines | R lines | O lines |
|---|---|---:|---:|---:|---:|---:|
| `docs/architecture/state_engine/proof-catalog/v3/catalog.json` | curated-proof-data | 34468 | 34468 | 34468 | 34468 | 34468 |
| `tests/unit/architecture/test_web_landscape_mutation_fencing.py` | authored-tests | 19572 | 22439 | 19572 | 22439 | 22439 |
| `tests/unit/web/composer/test_tools.py` | authored-tests | 21212 | 21212 | 21212 | 21226 | 21226 |
| `tests/unit/architecture/test_session_db_mutation_authority.py` | authored-tests | 19389 | 19910 | 19389 | 19910 | 19910 |
| `tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json` | fixture | 16523 | 16523 | 16523 | 16523 | 16523 |
| `tests/unit/web/sessions/test_routes.py` | authored-tests | 15361 | 15891 | 15361 | 15891 | 15891 |
| `tests/unit/web/composer/test_state.py` | authored-tests | 14062 | 14062 | 14062 | 14062 | 14062 |
| `docs/architecture/state_engine/assessments/2026-08-15-0537/assessment.json` | generated-report | 12459 | 12459 | 12459 | 12459 | 12459 |
| `tests/unit/web/execution/test_service.py` | authored-tests | 12067 | 12367 | 12067 | 12367 | 12367 |
| `tests/unit/engine/test_processor.py` | authored-tests | 11561 | 11561 | 11561 | 11561 | 11561 |
| `tests/integration/pipeline/test_composer_runtime_agreement.py` | authored-tests | 10106 | 10176 | 10106 | 10176 | 10176 |
| `src/elspeth/web/composer/state.py` | authored-source | 10054 | 10054 | 10054 | 10054 | 10054 |
| `examples/large_scale_test/input.csv` | data | 10001 | 10001 | 10001 | 10001 | 10001 |
| `tests/unit/web/composer/test_service.py` | authored-tests | 9584 | 9584 | 9584 | 9584 | 9584 |
| `tests/unit/web/composer/test_pipeline_planner.py` | authored-tests | 9383 | 9383 | 9383 | 9383 | 9383 |
| `src/elspeth/web/frontend/package-lock.json` | lock | 9164 | 9164 | 9164 | 9164 | 9164 |
| `docs/arch-analysis-2026-09-23-2016/02-subsystem-catalog.md` | docs | 8968 | 8968 | 8968 | 8968 | 8968 |
| `src/elspeth/web/sessions/service.py` | authored-source | 7247 | 8953 | 7247 | 8953 | 8953 |
| `config/cicd/enforce_tier_model/web.yaml` | curated-policy-config | 8882 | 8882 | 8882 | 8882 | 8882 |
| `tests/unit/core/landscape/test_database_clock_authority.py` | authored-tests | 8243 | 8243 | 8243 | 8243 | 8243 |
| `docs/plans/2026-09-20-composer-async-operations/supplemental-obligation-collections-2026-10-07.json` | generated-report | — | 8088 | — | 8088 | 8088 |
| `tests/unit/engine/test_executors.py` | authored-tests | 7432 | 7432 | 7432 | 7432 | 7432 |
| `tests/unit/web/execution/test_validation.py` | authored-tests | 6913 | 6913 | 6913 | 6913 | 6913 |
| `docs/architecture/state_engine/assessments/2026-08-15-0537/coverage-mapping.json` | curated-proof-data | 6853 | 6853 | 6853 | 6853 | 6853 |
| `package-lock.json` | lock | 6763 | 6763 | 6780 | 6763 | 6763 |
| `tests/unit/architecture/test_dag_scenario_corpus_contract.py` | authored-tests | 6729 | 6729 | 6729 | 6729 | 6729 |
| `config/cicd/enforce_tier_model/core.yaml` | curated-policy-config | 6541 | 6541 | 6541 | 6541 | 6541 |
| `tests/unit/web/composer/test_advisor_checkpoint.py` | authored-tests | 6310 | 6310 | 6310 | 6310 | 6310 |
| `tests/unit/core/test_dag.py` | authored-tests | 6292 | 6292 | 6292 | 6292 | 6292 |
| `tests/unit/web/blobs/test_service.py` | authored-tests | 6246 | 6250 | 6246 | 6250 | 6250 |
| `elspeth-lints/src/elspeth_lints/core/cli.py` | authored-source | 6110 | 6110 | 6110 | 6110 | 6110 |
| `src/elspeth/web/coordination/repository.py` | authored-source | 5807 | 5921 | 5807 | 5921 | 5921 |
| `tests/unit/core/test_config.py` | authored-tests | 5909 | 5909 | 5909 | 5909 | 5909 |
| `src/elspeth/engine/processor.py` | authored-source | 5523 | 5523 | 5523 | 5523 | 5523 |
| `tests/fixtures/dag_scenario_corpus/harness.py` | authored-test-tooling | 5408 | 5408 | 5408 | 5408 | 5408 |
| `src/elspeth/web/execution/service.py` | authored-source | 4853 | 5315 | 4853 | 5315 | 5315 |
| `src/elspeth/cli.py` | authored-source | 5278 | 5278 | 5278 | 5278 | 5278 |
| `uv.lock` | lock | 5276 | 5276 | 5276 | 5276 | 5276 |

## Classification evidence and handling

The six main authored source entries are Python application modules: their module docstrings name the CLI, row processor, composition state, coordination repository, session service, and lint CLI. PR281/repair add the seventh oversized source module, execution service. All eighteen oversized `test_*.py` entries contain executable pytest suites; the large architecture files also implement AST scanners and exact inventory pins, so treating them as inert generated data would be incorrect. `tests/fixtures/dag_scenario_corpus/harness.py` explicitly identifies itself as a “Production-path execution harness”; it is authored test tooling despite its fixture-directory location.

| Artifact path | Manual evidence | Handling |
|---|---|---|
| `docs/architecture/state_engine/proof-catalog/v3/catalog.json` | Normative catalog schema, reviewed applicability reasons, and plugin proof cases documented by its adjacent README and CONTRIBUTING catalog pins. | Curated proof data; preserve producer/consumer and byte pins. |
| `docs/architecture/state_engine/assessments/2026-08-15-0537/assessment.json` | Assessment schema and derived coverage fields; `scripts/state_engine_assessment_lib/package.py` writes the assessment document with JSON serialization. | Generated assessment report; no mechanical code split. |
| `docs/architecture/state_engine/assessments/2026-08-15-0537/coverage-mapping.json` | Description calls this reviewed node-to-cell provenance; records reviewers, assertion-basis rationales and exclusions. | Curated proof data; no mechanical code split. |
| `docs/plans/2026-09-20-composer-async-operations/supplemental-obligation-collections-2026-10-07.json` | Carries source digest, normalized source references, origin line references and derived title candidates. | Generated/compiled obligation report; source mapping is the maintenance unit. |
| `docs/arch-analysis-2026-09-23-2016/02-subsystem-catalog.md` | Header states it concatenates 25 written and reviewed slice entries with annotations. | Authored/assembled documentation; organize by substantive slices if needed. |
| `config/cicd/enforce_tier_model/{core,web}.yaml` | `allow_hits` contain human reasons, judge rationales and operator signature metadata. | Curated policy/config with generated bindings; not wholly generated. Use existing enumeration and operator signing workflow if reorganized. |
| `tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json` | Rule-keyed JSON arrays of fingerprint records consumed as a fixture. | Fixture/baseline; preserve the generator and comparison authority. |
| `examples/large_scale_test/input.csv` | Header plus example input records. | Data, not authored implementation; do not split for line-count optics. |
| `package-lock.json`, `src/elspeth/web/frontend/package-lock.json`, `uv.lock` | Dependency-resolution lock schemas (`lockfileVersion`/`packages`; `version`/`resolution-markers`). | Generated lock files; keep package-manager authority. |

No vendor or minified entry exceeds the threshold in these snapshots. This is the inventory category selection, not an assertion that the repository contains no vendor/minified material.

## Branch overlap and moving overlay

[changed-overlap.csv](changed-overlap.csv) lists every added, deleted or modified tracked path against the main commit, including a boolean for whether either version exceeds 5,000 lines. It compares blob identity and mode; a content change at the same line count still appears. The fixed branches are separate snapshots, not an assumption that one is a superset of another.

Oversized paths changed relative main:

| Path | P change | S change | R change | O change |
|---|---|---|---|---|
| `tests/unit/architecture/test_web_landscape_mutation_fencing.py` | modified | — | modified | modified |
| `tests/unit/web/composer/test_tools.py` | — | — | modified | modified |
| `tests/unit/architecture/test_session_db_mutation_authority.py` | modified | — | modified | modified |
| `tests/unit/web/sessions/test_routes.py` | modified | — | modified | modified |
| `tests/unit/web/execution/test_service.py` | modified | — | modified | modified |
| `tests/integration/pipeline/test_composer_runtime_agreement.py` | modified | — | modified | modified |
| `src/elspeth/web/sessions/service.py` | modified | — | modified | modified |
| `docs/plans/2026-09-20-composer-async-operations/supplemental-obligation-collections-2026-10-07.json` | added | — | added | added |
| `package-lock.json` | — | modified | — | — |
| `tests/unit/architecture/test_dag_scenario_corpus_contract.py` | modified | — | modified | modified |
| `tests/unit/web/blobs/test_service.py` | modified | — | modified | modified |
| `src/elspeth/web/coordination/repository.py` | modified | — | modified | modified |
| `src/elspeth/web/execution/service.py` | modified | — | modified | modified |
| `uv.lock` | modified | — | modified | modified |

Repair overlay raw observation:

```text
overlay repair-live-overlay: head=703cdd1674d01d422baa4d54f210105edccf498c tracked=6395 modified_vs_head=12 matching_byte_reread=True digest=af794ef26a47911d30e0af74842fa21cdd62786416b7ae472fc55476c6b4d04b
```

The overlay enumerates stage-0 tracked index paths, reads actual file bytes without following tracked symlinks, and rechecks HEAD/index plus a second byte pass. Both byte observations matched. This is not an atomic locked snapshot and does not prove that the worktree remained frozen after or between observations. It excludes untracked files and records actual raw-content object identities without writing objects.

Modified tracked regular paths relative the overlay HEAD (line counts can remain equal):

| Path | HEAD lines | Overlay lines |
|---|---:|---:|
| `CHANGELOG.md` | 3447 | 3457 |
| `config/cicd/masquerade_baseline.yaml` | 1880 | 1822 |
| `docs/plans/2026-09-20-composer-async-operations.md` | 273 | 281 |
| `gateway/README.md` | 381 | 385 |
| `gateway/conformance/test_capabilities.py` | 178 | 178 |
| `gateway/src/elspeth_llm_gateway/core/contract.py` | 454 | 454 |
| `gateway/tests/test_app.py` | 827 | 881 |
| `gateway/tests/test_config.py` | 465 | 465 |
| `gateway/tests/test_contract.py` | 718 | 743 |
| `docs/plans/2026-09-20-composer-async-operations/readiness-2026-10-06.md` | 176 | 188 |
| `docs/plans/2026-09-20-composer-async-operations/reported-bugs-2026-10-08.md` | 44 | 45 |
| `tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json` | 67 | 67 |

## Reproduce and reconstruct

Run from any ELSPETH checkout with these objects available. Set `OVERLAY` to the active repair checkout only when another moving sample is wanted.

```bash
ROOT=$(git rev-parse --show-toplevel)
python docs/refactoring/oversized-files-20261009/count_tracked_lines.py \
  --self-test --repo "$ROOT" \
  --snapshot main=9e481bc85a32ee15f808c5e2b16cab7634e57b8e \
  --snapshot published-pr281=9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc \
  --snapshot streaming-local=d2b73990d137e9725200c897544eebc5b958fdf4 \
  --snapshot repair-integrated=703cdd1674d01d422baa4d54f210105edccf498c \
  --output-dir "$OUTPUT_DIR"
# Optional: append --overlay "$OVERLAY" to include a live tracked sample.
```

[inventory.csv.gz](inventory.csv.gz) contains the complete main CSV inventory, compressed losslessly for the repository's `check-added-large-files --maxkb=1000` hook. The counting script still writes uncompressed `inventory.csv`; artifact packaging is a separate step. All three CSV writers use `lineterminator="\n"`. The retained gzip uses compression level 9 and `mtime=0`, without an embedded filename. The uncompressed CSV is 1,091,258 bytes; the gzip is 256,423 bytes. Decompression was measured byte-for-byte equal to the uncompressed CSV, and repeating compression with the same settings produced identical gzip bytes. The artifacts were normalized from the reviewed CRLF outputs to LF without resampling any snapshot or moving overlay. Every parsed CSV row remained exactly equal: 6,133 baseline rows and 1,726 rows in each of the two delta/overlap files.

```text
inventory.csv: reviewed_data_rows=6133 parsed_rows_equal=True LF_only=True before_bytes=1097392 after_bytes=1091258
snapshot-deltas.csv: reviewed_data_rows=1726 parsed_rows_equal=True LF_only=True before_bytes=352332 after_bytes=350605
changed-overlap.csv: reviewed_data_rows=1726 parsed_rows_equal=True LF_only=True before_bytes=243620 after_bytes=241893
original_bytes=1091258 gzip_bytes=256423 decompressed_equality=True deterministic_recompression=True
uncompressed_sha256=ee7034f10dafba31ec134891e0a1b864703ab29eb197ce6253dbe11f3cee73c9
gzip_sha256=18170504a677a17710377e853930bb715440a0b3896d03a139255ad6f5fd72a1
```

Read the retained baseline with `gzip -dc`, or open it directly with Python's `gzip.open`:

```bash
gzip -dc docs/refactoring/oversized-files-20261009/inventory.csv.gz > "$OUTPUT_DIR/inventory.csv"
# Package a fresh counting-script result deterministically when retaining it:
gzip -n -9 -c "$OUTPUT_DIR/inventory.csv" > "$OUTPUT_DIR/inventory.csv.gz"
```

```python
import csv
import gzip

with gzip.open("docs/refactoring/oversized-files-20261009/inventory.csv.gz", "rt", encoding="utf-8", newline="") as baseline:
    main_rows = {row["path"]: row for row in csv.DictReader(baseline)}
```

For another label, clone that path-keyed table and apply only matching rows from [snapshot-deltas.csv](snapshot-deltas.csv): remove `change=deleted`, replace/add the complete row otherwise, then set the snapshot label and commit on retained baseline rows. There are no transitive delta dependencies. [changed-overlap.csv](changed-overlap.csv) is a convenience projection of the same changes. [controls.txt](controls.txt) records reconstruction comparisons against fresh Git blob inventories for all four fixed commits.

Control evidence includes positive and negative threshold cases at 5,000/5,001 and 10,000/10,001; empty, unterminated, LF, CRLF, CR-only, Unicode separator, NUL and invalid UTF-8 cases; a real synthetic Git tree with a symlink/binary exclusion; and a mutation that appends a final unterminated line and changes selection from false to true at both thresholds. The final inventory/control process exited 0. Ruff check passed and the script was formatted. No pytest or broad lint corpus was run for this inventory artifact.

## Exclusions and provisional category coverage

All entries are inventoried even when a line count is unavailable. The strict UTF-8 rule also excludes files in another text encoding; exclusion is not permission to ignore an authored file. The excluded path/status sets below are identical in all five observations (measured by complete path-keyed equality); several binary byte sizes differ. Each observation contains `symlink=12`, `nul=11`, `non_utf8=1`, total `24`; there are no gitlinks or missing tracked entries in these observations. The one non-UTF-8 entry is the encrypted PDF fixture. Symlinks are excluded irrespective of target contents.

| Excluded path | Status | M bytes | P bytes | S bytes | R bytes | O bytes |
|---|---|---:|---:|---:|---:|---:|
| `.claude/skills/bug-sweep` | symlink | 30 | 30 | 30 | 30 | 30 |
| `.claude/skills/cicd-allowlist-audit` | symlink | 41 | 41 | 41 | 41 | 41 |
| `.claude/skills/config-contracts-guide` | symlink | 43 | 43 | 43 | 43 | 43 |
| `.claude/skills/elspeth-design` | symlink | 35 | 35 | 35 | 35 | 35 |
| `.claude/skills/engine-patterns-reference` | symlink | 46 | 46 | 46 | 46 | 46 |
| `.claude/skills/judge-signature-workflow` | symlink | 45 | 45 | 45 | 45 | 45 |
| `.claude/skills/lane-manager` | symlink | 33 | 33 | 33 | 33 | 33 |
| `.claude/skills/logging-telemetry-policy` | symlink | 45 | 45 | 45 | 45 | 45 |
| `.claude/skills/operating-aws-ecs-container` | symlink | 48 | 48 | 48 | 48 | 48 |
| `.claude/skills/operating-azure-container-apps` | symlink | 51 | 51 | 51 | 51 | 51 |
| `.claude/skills/orchestrator` | symlink | 33 | 33 | 33 | 33 | 33 |
| `.claude/skills/tier-model-deep-dive` | symlink | 41 | 41 | 41 | 41 | 41 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/checks-open-1280x720-chromium-linux.png` | nul | 148759 | 146093 | 146093 | 146093 | 146093 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/collapsed-authoring-1280x720-chromium-linux.png` | nul | 17823 | 17823 | 17823 | 17823 | 17823 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/populated-freeform-1280x720-chromium-linux.png` | nul | 75320 | 72446 | 72446 | 72446 | 72446 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/populated-freeform-1536x760-chromium-linux.png` | nul | 82154 | 78188 | 78188 | 78188 | 78188 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/populated-freeform-1920x1080-chromium-linux.png` | nul | 115489 | 113389 | 113389 | 113389 | 113389 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/populated-freeform-1920x900-chromium-linux.png` | nul | 97268 | 91850 | 91850 | 91850 | 91850 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/populated-freeform-1920x930-chromium-linux.png` | nul | 101410 | 97046 | 97046 | 97046 | 97046 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/populated-freeform-2560x1280-chromium-linux.png` | nul | 143785 | 138065 | 138065 | 138065 | 138065 |
| `src/elspeth/web/frontend/tests/e2e/composer-workspace.visual.spec.ts-snapshots/tall-dialog-1280x720-chromium-linux.png` | nul | 157412 | 153888 | 153888 | 153888 | 153888 |
| `tests/fixtures/pdf/encrypted_aes128_user_secret.pdf` | non_utf8 | 944 | 944 | 944 | 944 | 944 |
| `website/fonts/inter.woff2` | nul | 48256 | 48256 | 48256 | 48256 | 48256 |
| `website/fonts/jetbrains-mono.woff2` | nul | 31432 | 31432 | 31432 | 31432 | 31432 |

Every CSV category is a provisional path/schema classification outside the manually reviewed oversized set. For example, a resource beneath `src/` can receive an authored-source label even when it is an image or data resource; its independent `status` column still records exclusion correctly. These labels do not prove authorship or generation of all smaller resources. The following complete-tree category counters are output projections, not a refactoring target list.

| Provisional category | M entries | P entries | S entries | R entries | O entries |
|---|---:|---:|---:|---:|---:|
| authored-source | 1649 | 1701 | 1651 | 1703 | 1703 |
| authored-test-tooling | 1 | 1 | 1 | 1 | 1 |
| authored-tests | 2472 | 2578 | 2472 | 2578 | 2578 |
| authored-tooling | 110 | 110 | 110 | 110 | 110 |
| config-or-other | 502 | 502 | 502 | 502 | 502 |
| curated-policy-config | 47 | 47 | 47 | 47 | 47 |
| curated-proof-data | 2 | 2 | 2 | 2 | 2 |
| data | 87 | 87 | 87 | 87 | 87 |
| docs | 789 | 887 | 789 | 887 | 887 |
| fixture | 468 | 471 | 468 | 471 | 471 |
| generated-report | 1 | 2 | 1 | 2 | 2 |
| lock | 4 | 4 | 4 | 4 | 4 |
| minified | 1 | 1 | 1 | 1 | 1 |
