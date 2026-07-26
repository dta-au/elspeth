# DAG Corpus Wave A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` for implementation tasks. Apply
> `superpowers:test-driven-development`, `superpowers:systematic-debugging`,
> and `superpowers:verification-before-completion` at their normal gates.

**Goal:** Repair the open corpus evidence-integrity defects and add four
honest configuration-to-production-build cases without crossing the active
deferred-platform runtime boundary.

**Architecture:** Preserve the common manifest-driven harness. Add a typed
build-only workflow whose evidence stops after production graph/config
assembly, require every declared v1 input fixture to be consumed by the YAML
template, and make repository documentation-link validation containment-aware.

**Tech Stack:** Python 3.12/3.13, Pydantic v2, PyYAML, pytest, Elspeth's
production config/plugin/DAG/preflight APIs, Filigree, Loomweave, Warpline.

---

### Task 1: Close the unused-input evidence hole

**Filigree:** `elspeth-e8acea2a55`

**Files:**

- Modify: `tests/fixtures/dag_scenario_corpus/harness.py`
- Modify: `tests/integration/core/dag/test_dag_scenario_production_path.py`

1. Atomically start and advance the bug with a ticket-specific assignee.
2. Add a regression whose fixture is valid YAML but omits `${input_csv}`;
   observe that current rendering accepts it while hashing unrelated input
   bytes.
3. Make `render_settings` fail closed before substitution when the canonical
   input placeholder is absent.
4. Prove existing cases still render and the regression passes.
5. Run focused Ruff, format, and mypy checks; commit, comment, transition
   through verification, and close the bug.

### Task 2: Contain repository-relative documentation links

**Filigree:** `elspeth-d88d0e45c0`

**Files:**

- Modify: `tests/unit/architecture/test_dag_scenario_corpus_contract.py`

1. Atomically start and advance the bug with a ticket-specific assignee.
2. Add tests proving an existing `..` target outside the repository is
   reported invalid while a parent traversal that remains inside is accepted.
3. Resolve each decoded relative target and check `relative_to(REPOSITORY_ROOT)`
   before checking existence.
4. Run the focused contract suite and static checks; commit, comment,
   transition through verification, and close the bug.

### Task 3: Add the typed build-only evidence contract

**Files:**

- Modify: `tests/fixtures/dag_scenario_corpus/schema.py`
- Modify: `tests/fixtures/dag_scenario_corpus/harness.py`
- Modify: `tests/unit/architecture/test_dag_scenario_corpus_contract.py`
- Modify: `tests/integration/core/dag/test_dag_scenario_production_path.py`

1. Add failing model tests for build expectations and workflow/expectation
   mismatch, plus observed graph-shape serialization tests.
2. Add `build` to the workflow vocabulary, introduce an immutable
   `BuildExpectation`, and extend `GraphEvidence` with exact node-type counts
   and sorted edge labels.
3. Add `_build_case` returning completed stages `config`, `build` and explicit
   unattempted runtime/audit/recovery evidence.
4. Add a table-driven build-case test that compares every observed graph fact
   to its manifest expectation and proves Orchestrator/Landscape are not used.
5. Keep all existing run/recovery assertions green.

### Task 4: Register the bounded Wave A fixtures

**Files:**

- Modify: `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
- Modify: `docs/architecture/dag/scenario-corpus/README.md`
- Create: `tests/fixtures/dag_scenario_corpus/v1/multiple-independent-sources/independent-roots.yaml`
- Create: `tests/fixtures/dag_scenario_corpus/v1/multiple-independent-sources/input.csv`
- Create: `tests/fixtures/dag_scenario_corpus/v1/multi-source-queue-fan-in/queued-fan-in.yaml`
- Create: `tests/fixtures/dag_scenario_corpus/v1/multi-source-queue-fan-in/input.csv`
- Create: `tests/fixtures/dag_scenario_corpus/v1/conditional-routing/two-way-gate.yaml`
- Create: `tests/fixtures/dag_scenario_corpus/v1/conditional-routing/input.csv`
- Create: `tests/fixtures/dag_scenario_corpus/v1/fork-coalesce-policies/require-all-nested.yaml`
- Create: `tests/fixtures/dag_scenario_corpus/v1/fork-coalesce-policies/input.csv`
- Modify: `tests/unit/architecture/test_dag_scenario_corpus_contract.py`
- Modify: `tests/integration/core/dag/test_dag_scenario_production_path.py`

1. Add the four fixtures with `${input_csv}` and `${output_jsonl}` bindings.
2. Observe contract failures before registering the new cases and evidence.
3. Add exact build expectations and matching harness evidence records; attach
   each only to the config/build cells it directly strengthens.
4. Update exact case/evidence inventory assertions and the README workflow
   instructions. Do not promote runtime/audit/recovery cells.
5. Run all registered build, run, and recovery cases without skips or xfails.

### Task 5: Combined review, cleanup, and Wave B handoff

1. Run:

   ```bash
   env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
     tests/unit/architecture/test_dag_scenario_corpus_contract.py \
     tests/integration/core/dag/test_dag_scenario_production_path.py
   env -u VIRTUAL_ENV uv run --frozen ruff check \
     tests/fixtures/dag_scenario_corpus \
     tests/unit/architecture/test_dag_scenario_corpus_contract.py \
     tests/integration/core/dag/test_dag_scenario_production_path.py
   env -u VIRTUAL_ENV uv run --frozen ruff format --check \
     tests/fixtures/dag_scenario_corpus \
     tests/unit/architecture/test_dag_scenario_corpus_contract.py \
     tests/integration/core/dag/test_dag_scenario_production_path.py
   env -u VIRTUAL_ENV uv run --frozen mypy \
     tests/fixtures/dag_scenario_corpus \
     tests/unit/architecture/test_dag_scenario_corpus_contract.py \
     tests/integration/core/dag/test_dag_scenario_production_path.py
   git diff --check
   ```

2. Obtain fresh specification-compliance and code-quality reviews; resolve
   every finding and rerun the same checks.
3. Use Warpline to derive the advisory re-verification worklist and explicitly
   record unavailable enrichment.
4. Reconcile the parent issue and any other recorded claims whose current
   evidence supports close or claim release; do not disturb the active
   deferred-platform or operator P0 owners.
5. Write the Wave B handoff with the exact platform trigger SHA, remaining
   scenario cells/cases, rebase order, collision surfaces, and verification
   commands. Keep the parent corpus issue open unless its full acceptance is
   actually satisfied.
