# Reproducible State Engine Assessment Program

This is the executable operator guide for a state-engine assessment. Run every
command from the repository root unless a step says otherwise. The checked-in
`scripts/state_engine_assessment.py` command is the sole catalog and package
validator; do not copy its rules into a dated package or another document.

## Quick start

1. Read this page, the [criteria](completeness-criteria.md), the
   [architecture](architecture.md), and the
   [proof catalog](proof-catalog/README.md).
2. Initialize a full v2 package, or materialize an allowed delta from its
   parent.
3. Capture the exact baseline and environment before executing evidence.
4. Execute exact command vectors and retain honest limitations.
5. Have independent architecture, evidence, and future-agent readers review
   the package.
6. Run all five validator operations below. Update the hub only after material
   review findings are resolved.

## 1. Choose mode and initialize

Use local Canberra time in `YYYY-MM-DD-HHMM` form:

```bash
STATE_ASSESSMENT_ID="$(TZ=Australia/Canberra date '+%Y-%m-%d-%H%M')"
STATE_ASSESSMENT_DIR="docs/architecture/state_engine/assessments/${STATE_ASSESSMENT_ID}"
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  init-full "${STATE_ASSESSMENT_ID}" "${STATE_ASSESSMENT_DIR}"
```

The initializer creates a fully materialized 73-leg v2 manifest plus readable
README, evidence, and review templates. It refuses any pre-existing destination
(including a symlink), stages the complete package in a sibling directory, and
renames it into place only after every write succeeds.

Use `full` when the catalog, architecture, state vocabulary, transaction
boundaries, support profiles, or global verdict may change. A delta is also a
fully materialized manifest, never a sparse patch. It names
`parent_assessment` as `{"path": "relative/path/assessment.json", "sha256":
"..."}`, declares each changed proof cell in `changed_tuples`, and declares
changed gates in `changed_gate_ids`.

A v2 changed tuple contains `leg_id`, `dimension_id`, `case_id`, and
`profile_case`. Copy the parent, change the assessment/baseline identity, and
rerun or remove evidence for every affected cell. The validator compares
normalized cell status plus referenced evidence content and gate records to the
parent. Undeclared changes fail. A delta cannot change catalogs or declare the
global engine complete.

## 2. Capture Git identity

Prefer a clean dedicated worktree at the exact commit. Capture:

```bash
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
git branch --show-current
git remote get-url origin
git status --porcelain=v2 --branch --untracked-files=all
git worktree list --porcelain
git submodule status --recursive
```

If relevant uncommitted changes exist, stop and use a clean worktree or retain
the complete overlay outside the worktree before creating package files:

```bash
STATE_CAPTURE_DIR="$(mktemp -d)"
git status --porcelain=v2 --branch --untracked-files=all \
  >"${STATE_CAPTURE_DIR}/worktree-status.txt"
git diff --binary >"${STATE_CAPTURE_DIR}/unstaged.patch"
git diff --binary --cached >"${STATE_CAPTURE_DIR}/staged.patch"
git ls-files --others --exclude-standard -z \
  >"${STATE_CAPTURE_DIR}/untracked.paths.z"
tar --null --verbatim-files-from -czf "${STATE_CAPTURE_DIR}/untracked.tar.gz" \
  -T "${STATE_CAPTURE_DIR}/untracked.paths.z"
sha256sum "${STATE_CAPTURE_DIR}"/*
```

Retain the patches, path list, and archive when an untracked or modified file
can affect behavior. A hash-only list cannot reconstruct untracked content.
Never claim a dirty run is reproducible from a commit alone.

## 3. Capture environment identity

Record values, versions, paths, and SHA-256 digests without capturing secrets:

```bash
date --iso-8601=seconds
printf '%s\n' "${TZ-unset}" "${LANG-unset}" "${LC_ALL-unset}"
uname -a
.venv/bin/python --version
.venv/bin/python -c 'import multiprocessing, platform, sys; print(sys.executable); print(platform.python_build()); print(multiprocessing.get_start_method(allow_none=True))'
.venv/bin/python -m pytest --version
uv --version
git --version
.venv/bin/python -c 'import sqlite3, sqlalchemy; print(sqlite3.sqlite_version); print(sqlalchemy.__version__)'
sha256sum pyproject.toml uv.lock
```

The manifest records only explicitly safe environment name/value pairs needed
to reproduce an evidence command. Redact values at capture time; a hash of a
secret is still sensitive evidence and does not belong in the package.

## 4. Capture structural and tracker state

Use Loomweave only from the primary checkout. Record index identity,
freshness, commit, capture time, and any diagnostic limitations. Absence in a
stale or degraded index is not unreachability evidence.

Capture Filigree with exact non-truncated queries. At minimum retain:

```bash
filigree --version
filigree search '[state engine]' --json
filigree ready --json
filigree blocked --json
```

Prefer a full JSONL export when tracker identity is load-bearing. Record the
capture time, command/query, atomicity limitations, byte size, and SHA-256. A
truncated session-context screen is orientation, not canonical evidence.

## 5. Validate catalog identity

The current catalog is `proof-catalog/v2/catalog.json`. Validate its complete
contract and record its digest:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-catalog docs/architecture/state_engine/proof-catalog/v2/catalog.json
sha256sum docs/architecture/state_engine/proof-catalog/v2/catalog.json
```

This operation rejects duplicate JSON keys; wrong leg, dimension, gate, or
profile identity; invalid state-store/deployment/lifecycle references; runtime
enum drift; and a first-party plugin inventory that differs from live
discovery. Normal `json.load` alone is insufficient because it silently
overwrites duplicate keys.

The v2 applicability axis is explicit. Evidence coverage and overrides name an
exact `(leg_id, dimension_id, case_id, profile_case)` cell. A passing SQLite
cell does not promote the corresponding PostgreSQL cell, or vice versa.

## 6. Execute evidence

Run production-boundary evidence first, then direct repository detail. Use
argument vectors in `assessment.json`; the Markdown command is a readable
rendering, not the authority.

For pytest evidence, emit a JUnit artifact and record exact node collection:

```bash
STATE_EVIDENCE_ARTIFACTS="${STATE_ASSESSMENT_DIR}/artifacts"
STATE_EVIDENCE_NODES="${STATE_ASSESSMENT_DIR}/nodes"
mkdir -p "${STATE_EVIDENCE_ARTIFACTS}" "${STATE_EVIDENCE_NODES}"
.venv/bin/python -m pytest --collect-only -q -n 0 \
  tests/path/test_file.py::test_exact_node \
  >"${STATE_EVIDENCE_ARTIFACTS}/EV-001.collect.stdout" \
  2>"${STATE_EVIDENCE_ARTIFACTS}/EV-001.collect.stderr"
awk 'index($0, "::") && $0 !~ /^ / {print}' \
  "${STATE_EVIDENCE_ARTIFACTS}/EV-001.collect.stdout" \
  >"${STATE_EVIDENCE_NODES}/EV-001.txt"
.venv/bin/python -m pytest -q -n 0 \
  --junitxml="${STATE_EVIDENCE_ARTIFACTS}/EV-001.junit.xml" \
  tests/path/test_file.py::test_exact_node \
  >"${STATE_EVIDENCE_ARTIFACTS}/EV-001.stdout" \
  2>"${STATE_EVIDENCE_ARTIFACTS}/EV-001.stderr"
sha256sum \
  "${STATE_EVIDENCE_NODES}/EV-001.txt" \
  "${STATE_EVIDENCE_ARTIFACTS}"/EV-001.*
```

Record the exit code even when the command fails. Do not hide skips, xfails,
warnings, service absence, or nondeterministic timing.

## 7. Attach evidence to proof cells

Evidence may cover only the exact cells its assertions prove. Each record says
what it establishes and does not establish. Use these minimum standards:

- production entry: real caller, not helper-only construction;
- rollback: before/after images include scheduler, events, coordination,
  outcomes, branch loss, effects, attempts, and external visibility as
  applicable;
- concurrency: independent connections/processes with a bounded completion
  oracle;
- crash/restart: a fresh object/process against the same durable store;
- boundary composition: a representative supported plugin or orchestration
  boundary, not a mock-only/helper-only seam;
- read model: positive and negative truth-table arms, exact expiry boundary,
  owner/run scoping, deduplication, and ordering;
- profile case: the evidence actually uses the named backend/deployment pair.

PB-10 requires seven separate semantic cases for row-union release. TS-19,
RM-14, and F-14 require the durable `ABANDONED` contract to be distinguished
from decided terminal outcomes, ordinary pending work, and illegal re-entry.

## 8. Classify gaps and tracker ownership

Every unresolved cell records a reason, observable exit gate, and
`owner_issue`. Use `null` when genuinely unowned. Create or update a Filigree
issue for a coherent confirmed defect or actionable remediation theme; do not
create one issue per unknown proof cell. A closed evidence-package issue is
historical context, not the owner of broader residual work.

Filigree status is live and mutable. Store the issue ID and captured snapshot;
do not copy current assignment or priority into evergreen architecture prose.

## 9. Review and iterate

Dispatch independent readers for the architecture, evidence, and future-agent
lenses defined in the framework. Use `templates/review-record.md`. For each
material finding:

1. reproduce or inspect the cited evidence;
2. accept, reject, or narrow it with a concrete reason;
3. change the catalog, assessment, evidence, or prose as required;
4. rerun affected commands;
5. request re-review from a fresh reader.

Do not treat reviewer names, approvals, or signatures as correctness evidence.
The final review record must contain the exact line `Review outcome: complete`;
the package validator rejects a pending record.

## 10. Direct package validation

Before updating the hub, run the package, evidence-collection, and link checks:

```bash
STATE_ASSESSMENT_PATH="${STATE_ASSESSMENT_DIR}/assessment.json"
PYTHONPATH="$PWD/src" PYTHONOPTIMIZE=0 .venv/bin/python \
  scripts/state_engine_assessment.py validate-package "${STATE_ASSESSMENT_PATH}"
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  collect-evidence "${STATE_ASSESSMENT_PATH}"
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  check-links
git diff --check
```

`validate-package` rejects duplicate keys, namespace or profile drift, omitted
legs, unsupported evidence promotion/N/A, dangling coverage, invalid gate
mappings, dishonest derived verdicts/counts, altered artifacts or node indexes,
and a human proof matrix that contradicts the manifest. It resolves the
recorded baseline commit/tree and refuses any committed or uncommitted
difference outside `docs/` before live plugin discovery.

`collect-evidence` accepts only a literal `sys.executable -m pytest` command
with allowlisted collection options and explicit `tests/` selectors. It strips
JUnit output and `-n 0` arguments, clears ambient pytest options, disables
third-party plugin autoload, applies only the closed safe-environment
allowlist, enforces the positive timeout, and kills the collection process
group on expiry. The exact resulting node list must equal the retained index.
`check-links` checks repository-relative Markdown links under the state-engine
documentation and `docs/README.md`; absolute paths, repository escapes, and
symlink inputs/escapes are invalid.
These are direct assessment operations, not unit tests for a document package.

## Historical rerun

### Strict v1 rerun

The v1 catalog and its 68-leg packages are immutable historical evidence. Use
this path only when an assessment has a v1 manifest and recorded hashes:

1. Verify the v1 catalog digest, assessment manifest, node indexes, retained
   overlays, and evidence artifact hashes that exist in the package-bearing
   checkout.
2. Create a separate detached worktree at the recorded behavioral commit and a
   worktree-local environment. Never use the primary checkout's editable
   installation for a historical rerun.
3. Reconstruct a recorded behavioral overlay, if any, and verify its digest.
4. Compare environment facts, then execute the literal recorded argument
   vectors. Rewrite only output transport (`--junitxml`, stdout, and stderr) to
   a new `reruns/<timestamp>/` directory; preserve every selector and behavioral
   option.
5. Apply each record's `safe_environment`, timeout, and relative working
   directory. Treat historical tracker/index captures as evidence and new live
   state as a separate observation.
6. Compare deterministic artifacts by hash and logs/JUnit by declared semantic
   fields. Write a divergence report without modifying the original assessment
   or retained artifacts.

The current v2 catalog intentionally fails live-inventory validation against an
older v1 checkout, and the v1 catalog may fail live-inventory validation against
current source. Validate historical identity in the package-bearing checkout;
execute historical behavior in the detached baseline worktree.

### Legacy best-effort reconstruction

Pre-v1 packages may have no catalog, manifest, node index, overlay, or artifact
hashes. Do not pretend those fields exist. Recover the named Git document with
`git show <commit>:<path>`, create a detached worktree, execute only literal
commands preserved in the package, and mark every missing identity or artifact
`unreproducible`. Store the rerun and divergence report separately; never
rewrite a legacy result into v1 or v2 form.

## Failure handling

- `SCHEMA_MISMATCH`: stop and surface the Filigree upgrade guidance.
- stale Loomweave: refresh before structural claims.
- missing external credentials: mark affected cells unknown; do not infer pass.
- dirty unrecorded worktree: stop or capture the complete overlay.
- interrupted command: retain partial output as failed evidence and rerun under
  a new evidence ID.
- contradictory source/docs/tracker: open `HG-10`; do not choose the convenient
  surface silently.
