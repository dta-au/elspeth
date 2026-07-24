# Out-of-Box Example Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every in-scope example runnable from a clean checkout through one documented command, beginning with a self-contained offline blob-transform launcher, and verify the complete non-Azure/non-PostgreSQL/non-endurance matrix for release 0.7.2.

**Architecture:** Standalone examples keep their direct settings command; setup-bearing examples own setup in a fail-fast `run.sh`. The blob-transform example packages its source fixtures locally, generates only ignored runtime state, and receives a clean-copy end-to-end regression. The work runs directly in the exclusive-custody `release/0.7.2` checkout, as requested by the repository owner.

**Tech Stack:** Bash, Python 3.13, pytest, Typer `CliRunner`, CSV fixtures, ELSPETH CLI, SQLite, Docker, ChaosLLM/ChaosWeb, ChromaDB, OpenRouter.

---

## File Map

- Create `examples/blob_transforms/input/feed_a.csv`: first packaged CSV blob fixture.
- Create `examples/blob_transforms/input/feed_b.csv`: second packaged CSV blob fixture.
- Create `examples/blob_transforms/run.sh`: canonical offline preparation and execution entry point.
- Modify `examples/blob_transforms/scripts/prepare_csv_blob_manifest.py`: remove the sibling-example fixture dependency.
- Modify `examples/blob_transforms/README.md`: document the one-command offline path and generated artifacts.
- Modify `examples/README.md`: identify the blob example's canonical launcher.
- Modify `examples/AGENTS.md`: make the agent-facing run instruction match the user-facing command.
- Modify `tests/e2e/examples/test_shipped_examples.py`: prove the launcher works from a clean copied example and that all three catalogs advertise it.

No production module changes are planned. If validation reveals a separate runtime defect, stop that matrix lane, add a focused red regression at the owning layer, and amend this plan before changing production code.

### Task 1: Add the clean-copy launcher contract

**Files:**
- Modify: `tests/e2e/examples/test_shipped_examples.py`

- [ ] **Step 1: Add the required standard-library imports**

Add `csv` and `subprocess` alongside the existing imports:

```python
import csv
import json
import shutil
import subprocess
```

- [ ] **Step 2: Add the failing clean-copy execution test**

Append this test to `TestShippedExamples`:

```python
def test_blob_transform_offline_launcher_runs_from_clean_copy(
    self,
    example_pipeline_dir: Path,
    tmp_path: Path,
) -> None:
    """The canonical offline blob example owns all local preparation."""
    example_dir = self._copy_example_to_tmp(example_pipeline_dir, tmp_path, "blob_transforms")
    repository_venv = example_pipeline_dir.parent / ".venv"
    assert repository_venv.is_dir(), "documented repository environment is missing"
    (tmp_path / ".venv").symlink_to(repository_venv, target_is_directory=True)

    result = subprocess.run(
        ["bash", str(example_dir / "run.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    manifest_path = example_dir / "input" / "csv_blob_manifest.csv"
    with manifest_path.open(newline="", encoding="utf-8") as manifest_file:
        manifest_rows = list(csv.DictReader(manifest_file))
    assert [row["source_name"] for row in manifest_rows] == ["feed_a", "feed_b"]

    payload_files = [
        path
        for path in (example_dir / "payloads").rglob("*")
        if path.is_file() and path.name != ".gitkeep"
    ]
    assert len(payload_files) == 2
    assert (example_dir / "runs" / "audit.db").is_file()

    with (example_dir / "output" / "expanded_csv_rows.csv").open(newline="", encoding="utf-8") as output_file:
        output_rows = list(csv.DictReader(output_file))
    assert len(output_rows) == 200
    assert Counter(row["source_name"] for row in output_rows) == Counter({"feed_a": 100, "feed_b": 100})
```

- [ ] **Step 3: Add the failing documentation-contract test**

Append this test to the same class:

```python
def test_blob_transform_documents_canonical_launcher(self, example_pipeline_dir: Path) -> None:
    """Every example catalog points to the supported one-command entry point."""
    command = "./examples/blob_transforms/run.sh"
    blob_readme = (example_pipeline_dir / "blob_transforms" / "README.md").read_text()
    examples_readme = (example_pipeline_dir / "README.md").read_text()
    agent_guide = (example_pipeline_dir / "AGENTS.md").read_text()

    assert command in blob_readme
    assert command in examples_readme
    assert command in agent_guide
```

- [ ] **Step 4: Run both tests and verify the contract is red**

Run:

```bash
.venv/bin/pytest \
  tests/e2e/examples/test_shipped_examples.py::TestShippedExamples::test_blob_transform_offline_launcher_runs_from_clean_copy \
  tests/e2e/examples/test_shipped_examples.py::TestShippedExamples::test_blob_transform_documents_canonical_launcher \
  -v
```

Expected: both tests fail because `examples/blob_transforms/run.sh` and its documented command do not exist.

### Task 2: Package the blob fixtures locally

**Files:**
- Create: `examples/blob_transforms/input/feed_a.csv`
- Create: `examples/blob_transforms/input/feed_b.csv`
- Modify: `examples/blob_transforms/scripts/prepare_csv_blob_manifest.py`

- [ ] **Step 1: Copy the existing deterministic fixtures into the owning example**

Mechanically copy the tracked 100-row fixture files without changing their bytes:

```bash
cp examples/multi_worker_showcase/input/feed_a.csv examples/blob_transforms/input/feed_a.csv
cp examples/multi_worker_showcase/input/feed_b.csv examples/blob_transforms/input/feed_b.csv
cmp examples/multi_worker_showcase/input/feed_a.csv examples/blob_transforms/input/feed_a.csv
cmp examples/multi_worker_showcase/input/feed_b.csv examples/blob_transforms/input/feed_b.csv
```

Expected: both `cmp` commands exit zero. These are independent packaged fixtures after the copy; later changes to the showcase must not silently alter the blob example.

- [ ] **Step 2: Point the generator only at its local fixtures**

Replace `INPUTS` in `prepare_csv_blob_manifest.py` with:

```python
INPUTS = (
    ("feed_a", EXAMPLE_DIR / "input" / "feed_a.csv"),
    ("feed_b", EXAMPLE_DIR / "input" / "feed_b.csv"),
)
```

- [ ] **Step 3: Verify the generator is self-contained in a copied example**

Run:

```bash
scratch_dir="$(mktemp -d)"
trap 'rm -rf -- "$scratch_dir"' EXIT
mkdir -p "$scratch_dir/examples"
cp -a examples/blob_transforms "$scratch_dir/examples/blob_transforms"
PYTHONPATH=src .venv/bin/python "$scratch_dir/examples/blob_transforms/scripts/prepare_csv_blob_manifest.py"
test -f "$scratch_dir/examples/blob_transforms/input/csv_blob_manifest.csv"
find "$scratch_dir/examples/blob_transforms/payloads" -type f ! -name .gitkeep | wc -l
```

Expected: the helper reports two stored blobs, the manifest exists, and the final count is `2`. Remove the explicit `scratch_dir` after recording the result.

### Task 3: Add the fail-fast canonical launcher

**Files:**
- Create: `examples/blob_transforms/run.sh`

- [ ] **Step 1: Add the launcher**

Create this executable script:

```bash
#!/usr/bin/env bash
# Blob CSV expansion example: prepare local payload blobs, then run the pipeline.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
ELSPETH_BIN="${ELSPETH_BIN:-$PROJECT_ROOT/.venv/bin/elspeth}"
cd "$PROJECT_ROOT"

mkdir -p \
  examples/blob_transforms/input \
  examples/blob_transforms/output \
  examples/blob_transforms/payloads \
  examples/blob_transforms/runs
rm -f \
  examples/blob_transforms/input/csv_blob_manifest.csv \
  examples/blob_transforms/output/expanded_csv_rows.csv \
  examples/blob_transforms/output/expansion_failures.jsonl \
  examples/blob_transforms/runs/audit.db \
  examples/blob_transforms/runs/audit.db-shm \
  examples/blob_transforms/runs/audit.db-wal
find examples/blob_transforms/payloads -mindepth 1 -type f ! -name .gitkeep -delete
find examples/blob_transforms/payloads -mindepth 1 -type d -empty -delete

echo "Preparing local CSV payload blobs..."
"$PYTHON_BIN" examples/blob_transforms/scripts/prepare_csv_blob_manifest.py

echo "Running offline blob CSV expansion..."
"$ELSPETH_BIN" run \
  --settings examples/blob_transforms/settings_expand_csv_blobs.yaml \
  --execute

echo "Output: examples/blob_transforms/output/expanded_csv_rows.csv"
echo "Audit: examples/blob_transforms/runs/audit.db"
```

- [ ] **Step 2: Mark the launcher executable**

Run:

```bash
chmod 755 examples/blob_transforms/run.sh
```

- [ ] **Step 3: Run the execution regression**

Run:

```bash
.venv/bin/pytest \
  tests/e2e/examples/test_shipped_examples.py::TestShippedExamples::test_blob_transform_offline_launcher_runs_from_clean_copy \
  -v
```

Expected: PASS with 200 output rows, two source names, two payload files, and a local audit database.

### Task 4: Publish the one-command documentation

**Files:**
- Modify: `examples/blob_transforms/README.md`
- Modify: `examples/README.md`
- Modify: `examples/AGENTS.md`

- [ ] **Step 1: Replace the two-step offline instructions**

In `examples/blob_transforms/README.md`, replace the separate preparation and execution commands with:

````markdown
Run the self-contained offline example from the repository root:

```bash
./examples/blob_transforms/run.sh
```

The launcher clears only this example's generated offline artifacts, stores the
two packaged CSV fixtures in the local payload store, writes the blob manifest,
and executes `settings_expand_csv_blobs.yaml`.
````

Keep the hosted tutorial HTML command unchanged and label it as an opt-in network example.

- [ ] **Step 2: Update the main example catalog**

Change the `blob_transforms` description in `examples/README.md` to:

```markdown
| [`blob_transforms`](blob_transforms/) | Blob-backed ingestion: self-contained offline CSV expansion via `./examples/blob_transforms/run.sh`, plus an opt-in hosted tutorial HTML fetch |
```

- [ ] **Step 3: Update the agent run guide**

Change the `blob_transforms` row in `examples/AGENTS.md` to:

```markdown
| `blob_transforms` | 200 offline expansion rows | Run `./examples/blob_transforms/run.sh`; it packages local fixtures into the payload store before executing. The hosted HTML fetch remains opt-in. |
```

- [ ] **Step 4: Run the documentation-contract test**

Run:

```bash
.venv/bin/pytest \
  tests/e2e/examples/test_shipped_examples.py::TestShippedExamples::test_blob_transform_documents_canonical_launcher \
  -v
```

Expected: PASS.

### Task 5: Verify and commit the packaging repair

**Files:**
- Test: `tests/e2e/examples/test_shipped_examples.py`
- Verify all files listed in the File Map.

- [ ] **Step 1: Run the complete shipped-example regression file**

Run:

```bash
.venv/bin/pytest tests/e2e/examples/test_shipped_examples.py -v
```

Expected: all tests pass, including the clean-copy launcher execution.

- [ ] **Step 2: Run changed-file quality gates**

Run:

```bash
.venv/bin/ruff check tests/e2e/examples/test_shipped_examples.py examples/blob_transforms/scripts/prepare_csv_blob_manifest.py
.venv/bin/ruff format --check tests/e2e/examples/test_shipped_examples.py examples/blob_transforms/scripts/prepare_csv_blob_manifest.py
git diff --check
pre-commit run --files \
  tests/e2e/examples/test_shipped_examples.py \
  examples/blob_transforms/scripts/prepare_csv_blob_manifest.py \
  examples/blob_transforms/run.sh \
  examples/blob_transforms/input/feed_a.csv \
  examples/blob_transforms/input/feed_b.csv \
  examples/blob_transforms/README.md \
  examples/README.md \
  examples/AGENTS.md
```

Expected: all commands exit zero.

- [ ] **Step 3: Commit the focused repair**

Run:

```bash
git add \
  tests/e2e/examples/test_shipped_examples.py \
  examples/blob_transforms/scripts/prepare_csv_blob_manifest.py \
  examples/blob_transforms/run.sh \
  examples/blob_transforms/input/feed_a.csv \
  examples/blob_transforms/input/feed_b.csv \
  examples/blob_transforms/README.md \
  examples/README.md \
  examples/AGENTS.md
git commit -m "fix(examples): package blob transform launcher"
```

Expected: commit succeeds with pre-commit hooks green.

### Task 6: Run every pure-data configuration

**Files:**
- No tracked file changes expected.

- [ ] **Step 1: Reset generated audit state**

Run:

```bash
./examples/reset.sh
```

Expected: exit zero and only ignored databases/vector stores are removed.

- [ ] **Step 2: Run the ordinary direct-settings examples**

For each exact file listed below, run `.venv/bin/elspeth run --settings examples/threshold_gate/settings.yaml --execute`, replacing only the settings argument with the next literal file and resetting that example's `runs/*.db*` before another configuration in the same directory:

```text
examples/threshold_gate/settings.yaml
examples/boolean_routing/settings.yaml
examples/explicit_routing/settings.yaml
examples/error_routing/settings.yaml
examples/deep_routing/settings.yaml
examples/batch_aggregation/settings.yaml
examples/deaggregation/settings.yaml
examples/json_explode/settings.yaml
examples/schema_contracts_demo/settings.yaml
examples/transform_pipeline/settings.yaml
examples/audit_export/settings.yaml
examples/report_assemble/settings.yaml
examples/retention_purge/settings.yaml
examples/landscape_journal/settings.yaml
examples/multi_flow/settings.yaml
examples/multi_source_queue/settings.yaml
examples/concurrent_scheduler/settings.yaml
examples/checkpoint_resume/settings.yaml
```

Expected: every command exits zero with a completed run.

- [ ] **Step 3: Run every fork/coalesce variant**

Run the following configurations separately:

```text
examples/fork_coalesce/settings.yaml
examples/fork_coalesce/settings_per_branch.yaml
examples/fork_coalesce/settings_union_first_wins.yaml
examples/fork_coalesce/settings_union_last_wins.yaml
```

Expected: all four exit zero. Then run `settings_union_fail.yaml`; expected exit is `4` with the documented union-collision failure. Any other outcome fails the example.

- [ ] **Step 4: Run all ten statistical batch configurations**

Run every `examples/statistical_batch_plugins/settings_*.yaml` except no exclusions apply in that directory:

```text
settings_classifier_metrics.yaml
settings_data_quality_report.yaml
settings_distribution_profile.yaml
settings_drift_compare.yaml
settings_effect_size.yaml
settings_experiment_compare.yaml
settings_outlier_annotator.yaml
settings_paired_preference.yaml
settings_threshold_summary.yaml
settings_top_k.yaml
```

Expected: all ten exit zero.

### Task 7: Run setup-bearing local examples

**Files:**
- No tracked file changes expected.

- [ ] **Step 1: Run the repaired blob example and hosted fetch**

Run:

```bash
./examples/blob_transforms/run.sh
./examples/reset.sh
.venv/bin/elspeth run --settings examples/blob_transforms/settings_fetch_tutorial_html.yaml --execute
```

Expected: both runs exit zero; the offline output has 200 rows and the hosted fetch produces three blob-ref rows.

- [ ] **Step 2: Run the SQLite database-sink launcher**

Run:

```bash
./examples/database_sink/run.sh
```

Expected: exit zero, four high-value rows, and a populated target-side effect ledger. Do not run a PostgreSQL variant.

- [ ] **Step 3: Run the Chroma retrieval examples**

Run:

```bash
./examples/chroma_rag/run.sh
./examples/reset.sh
.venv/bin/elspeth run --settings examples/chroma_rag_indexed/query_pipeline.yaml --execute
```

Expected: both exit zero; the indexed query automatically runs its indexing dependency.

- [ ] **Step 4: Run the tracked 10,000-row large-scale input**

Run:

```bash
.venv/bin/elspeth run --settings examples/large_scale_test/settings.yaml --execute
```

Expected: exit zero with 10,000 processed rows. Do not regenerate the 50,000-row benchmark input.

- [ ] **Step 5: Build and run the container example**

Run:

```bash
docker build -t elspeth:0.7.2-examples .
docker run --rm \
  -v "$PWD/examples/threshold_gate_container:/app/pipeline" \
  elspeth:0.7.2-examples \
  run --settings /app/pipeline/settings.yaml --execute
```

Expected: image build and pipeline execution both exit zero and write the two host-mounted outputs.

### Task 8: Run local fault-injection and worker examples

**Files:**
- No tracked file changes expected.

- [ ] **Step 1: Run the self-starting ChaosLLM sentiment example**

Run:

```bash
./examples/chaosllm_sentiment/run.sh
```

Expected: launcher starts and stops ChaosLLM, exits zero, and records completed or deliberately quarantined rows according to the configured fault profile.

- [ ] **Step 2: Run the rate-limited example against a bounded local server**

Start `.venv/bin/chaosllm serve --port 8199 --preset realistic --workers 1`, wait for `http://127.0.0.1:8199/health`, run:

```bash
.venv/bin/elspeth run --settings examples/rate_limited_llm/settings.yaml --execute
```

Expected: exit zero. Stop and reap the exact ChaosLLM PID afterward, even on failure.

- [ ] **Step 3: Run ChaosWeb against a bounded local server**

Start `.venv/bin/chaosweb serve --port 8200 --preset realistic --workers 1`, wait for `http://127.0.0.1:8200/health`, run:

```bash
.venv/bin/elspeth run --settings examples/chaosweb/settings.yaml --execute
```

Expected: exit zero with successes and configured fault routes represented. Stop and reap the exact ChaosWeb PID afterward.

- [ ] **Step 4: Run both multi-worker launchers**

Run:

```bash
./examples/multi_worker/run.sh
./examples/multi_worker_showcase/run.sh
```

Expected: the first prints its two-worker PASS assertion; the showcase exits zero and prints its demonstrative worker card. A stochastic invalid model response is rerun once from a clean example state; a reproducible failure becomes a tracked defect and focused regression.

The explicitly excluded `examples/chaosllm_endurance/run.sh` is not run.

### Task 9: Run every non-endurance OpenRouter configuration

**Files:**
- No tracked file changes expected.

- [ ] **Step 1: Load the key without printing it**

Run in a shell that sources `.env` with automatic export, then verify only presence:

```bash
set -a
source .env
set +a
test -n "${OPENROUTER_API_KEY:-}"
```

Expected: exit zero. Never echo or log the key.

- [ ] **Step 2: Run the ordinary OpenRouter configurations**

Run each configuration separately, clearing only that example's ignored run/output artifacts between configurations:

```text
examples/openrouter_sentiment/settings.yaml
examples/openrouter_sentiment/settings_pooled.yaml
examples/template_lookups/settings.yaml
examples/openrouter_multi_query_assessment/settings.yaml
examples/openrouter_multi_query_assessment/settings_journal.yaml
examples/openrouter_multi_query_assessment/settings_overflow.yaml
examples/schema_contracts_llm_assessment/settings.yaml
```

Expected: each exits zero; each run has terminal output or documented quarantine evidence rather than an infrastructure/authentication failure.

- [ ] **Step 3: Run the OpenRouter-backed RAG launcher**

Run:

```bash
./examples/chroma_rag_qa/run.sh
```

Expected: exit zero with retrieval context and LLM answers.

Do not run `examples/openrouter_multi_query_assessment/settings_stress.yaml`; it is the explicitly excluded OpenRouter endurance/stress workload.

### Task 10: Final release verification and tracker closeout

**Files:**
- No additional tracked changes expected unless a matrix defect required a separately reviewed TDD fix.

- [ ] **Step 1: Re-run the complete shipped-example test package**

Run:

```bash
.venv/bin/pytest tests/e2e/examples/test_shipped_examples.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run the full Python suite on the final commit**

Run:

```bash
.venv/bin/pytest
```

Expected: the suite passes with only the repository's documented skips and warnings. Record exact pass, skip, warning, duration, and commit SHA.

- [ ] **Step 3: Confirm a clean tracked worktree**

Run:

```bash
git status --short --branch
git log -3 --oneline
```

Expected: no uncommitted tracked changes. Ignored example outputs may remain and are reported or explicitly removed from their narrow example directories.

- [ ] **Step 4: Record the matrix in Filigree**

Capture the final commit and add a comment to release task `elspeth-64c319bf4d`:

```bash
final_sha="$(git rev-parse HEAD)"
filigree add-comment elspeth-64c319bf4d \
  "0.7.2 example verification complete at ${final_sha}: 53 in-scope entry points/configurations validated; fork/coalesce union-fail produced its expected exit 4. Skipped by owner instruction: Azure examples, PostgreSQL variants, ChaosLLM endurance, and OpenRouter stress. Clean-copy blob launcher regression and full pytest suite are green. Release publication remains blocked only by the separately recorded operator gate." \
  --actor codex-release-072
```

Expected: the comment is recorded against the release task. Do not close the release task while its operator blocker remains open.
