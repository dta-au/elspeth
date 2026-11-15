# 0.8.14 integration hand-off (TEMPORARY — delete this directory before merging)

Written 2026-11-11 by the "8. Python 3.14 Upgrade - .14" session, which is the
0.8.14 integrator, for its own continuation on a server whose GitHub credential
has the `workflow` scope. This directory exists only because that session could
not push changes under `.github/workflows/`. Remove it in the same push that
lands the patches.

## State of `claude/zen-turing-pphown`

Parent of the commit that added this directory: `21e74acc`.

- `75f44b85` — head of `claude/lucid-lamport-wank6h` (0.8.13, PR #362).
- Eight commits: Python 3.14 only (interpreter fixes, packaging and lockfiles,
  release and gateway images, docs, the asyncio log scrubber, lint tests) and
  the 0.8.14 release cut (27 version surfaces, `## 0.8.14` changelog section).
- `d3eb3222` — gateway: drop the krb5-libs workaround the 3.14 Alpine base
  does not need.
- `bec90dde`, `21e74acc` — session #9's Azure plugin work
  (`azure_content_understanding`, `azure_document_intelligence` hardening).

## First job: land the four workflow commits

```bash
git checkout claude/zen-turing-pphown
git am handoff/0.8.14-workflow-patches/*.patch   # applies cleanly on 21e74acc
git rm -r handoff && git commit -m "chore: remove the 0.8.14 integration hand-off"
git push origin claude/zen-turing-pphown
```

`git am` must be run before the hand-off commit is removed or rebased away; if
this directory's commit is still the tip, drop it first (`git reset --hard
21e74acc` after copying the patches out) or apply on top and remove afterwards.
The tree `21e74acc` + the four patches is byte-identical to the tree the full
gate below was run on.

1. `ci: run a single Python 3.14 lane`
2. `test(ci): pin the single-lane long-run proof matrix`
3. `ci: remove the uncovered test steps the 3.12 lane used`
4. `ci(live-provider): add the azure_content_understanding case` (specified by
   session #9)

Until they land, the branch requires Python >=3.14 while its workflows still
run 3.12/3.13 lanes, and `tests/unit/cicd` fails on the published tip.

## Verification so far (Python 3.14.6, 4-core cloud container)

On the full tree (21e74acc + four patches): ruff, mypy (1140 files),
`scripts/check_contracts.py`, actionlint 1.7.12 clean; `tests/unit/cicd`,
`tests/unit/plugins/transforms/azure`, docs, website, state-engine catalog and
DAG corpus contract: 2227 passed, 1 failed (`test_adr_public_integrity`).
Gateway suite: 720 passed. Both images built and smoke-tested earlier on the
0.8.12-based tree (release image ran `examples/boolean_routing` to COMPLETED);
not rebuilt since the rebases.

A full gate (`scripts/full-suite-gate.sh`, stages ruff,mypy,contracts,lints,
pytest) was started on that tree and was at 19% of pytest when this was
written; its result is NOT known and does not travel with the repo. Re-run it.
The keyless lint stage reported 3657 findings; that count has not been diffed
against 75f44b85.

Failures that pre-date this work in the cloud container (also fail on 3.13 on
the 0.8.12 base; re-check on the new server, several are environmental):
`tests/integration/web/test_composer_https_acceptance.py` (15),
`test_composer_react_generation_acceptance.py` (2),
`tests/unit/docs/test_adr_public_integrity.py` (needs full git history),
`tests/unit/test_node_runtime_contract.py`,
`tests/unit/test_mock_discipline_baseline.py`,
`tests/unit/deployment/test_aws_ecs_terraform_package.py` (2 on the 0.8.12
base; they passed after the 0.8.14 cut), `tests/unit/core/dag/
test_canonical_hash_corpus.py` (2), one `tests/e2e/examples` case. Two
load-sensitive flakes pass with `-n 0`: `test_pipeline_planner.py::
test_sync_planner_phases_run_off_loop_and_terminate_responsively` and
`test_ci_sharding.py::test_manifest_binds_finalized_real_pytest_cov_database`.

Not run at all: the PostgreSQL testcontainer suite, live providers, browser
end-to-end, and CI itself on the new single lane.

## Still to integrate

- Session #10 "Plugin Refresh" (`session_01Szt2FzsUgYrx4HhHYEBbKd`), branch
  `claude/compassionate-brahmagupta-o0nhlw`: one verified merge per fix, on
  Python 3.14. It was told to rebase onto `21e74acc`; nothing was pushed when
  this was written. Its later lanes re-pin the DAG scenario corpus and touch
  `plugins/transforms/azure/document_intelligence.py` (conflicts with #9).
  Plugin counts, state-engine catalog hashes and the soft-mapping census
  (2668) must be re-measured, not hand-merged, if a plugin is added or removed.
- Undecided by the owner: whether "7. Executor Profiling" (branch
  `claude/upbeat-planck-33a7pd`) and "11. Tool Remediation II" also land here.
- Session #9 (`session_01Szt2G1ffo7ivNtN9SNertZ`) is finished.

## Owner decisions and obligations on record

- 3.14 only; the gateway is in scope; ruff stays on `py313` (py314 would
  reformat 169 files to PEP 758 `except A, B:`).
- asyncio diagnostics: logger filter (`install_asyncio_diagnostic_scrubber`),
  class plus frame locations; startup refuses `JSON_LOGS=true`. The design
  review is in `asyncio-shield-options-report.md` beside this file.
- No lint signing for now. Four signed tier-model entries for
  `contracts/runtime_val_manifest.py` drift.
- The repository ruleset's required checks must be renamed to the 3.14 job
  names (`Test (Python 3.14, shard N)`, `Timing-sensitive tests (Python 3.14)`)
  before merge.
- Runs checkpointed and web approvals recorded under 3.13 do not carry over to
  3.14; drain before upgrading a deployment.
- The release image pins CPython 3.14.5, the newest the pinned uv (0.11.15)
  offers; a later patch needs a uv image bump.
- One masquerade test stays skipped on 3.14 (pre-PEP 649 annotation-order
  model).
- PR #362 (0.8.13) must merge first, or this branch carries it.
