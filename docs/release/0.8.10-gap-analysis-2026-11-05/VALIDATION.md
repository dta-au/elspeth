# Validation and qualification guide

Updated 6 November for source `ae88147c6130051b1e3d497a1341a69bd56924e5`.
These are instructions for the receiving implementation agent. No application
test commands here were executed by this audit; existing raw R220 evidence was
read and bound to current production/test/configuration bytes. Read the
current repository instructions before running them; selectors and ownership
may have changed after the audited commit.

## 1. Rebind source and custody

From the actual repository/worktree root, explicitly change directory before
commands. Capture HEAD, branch and ordinary short status. Inspect relevant
changes since the audit with native Git, including current uncommitted work.
Do not overwrite, clean, stage or stash another owner's files.

```bash
cd <repo-root> && git rev-parse HEAD
cd <repo-root> && git --no-optional-locks status --short --branch --untracked-files=normal
cd <repo-root> && git diff --name-status ae88147c6130051b1e3d497a1341a69bd56924e5..HEAD
cd <repo-root> && git show ae88147c6130051b1e3d497a1341a69bd56924e5:src/elspeth/web/composer/continuation_action_fit_yield.py
```

Placeholders such as `<repo-root>` must be replaced, not pasted literally into
a shell. Use a dedicated implementation worktree when the shared checkout is
dirty. Verify both import roots resolve inside that tree. Do not run a bare
installer through a symlinked shared `.venv`. Keep logs under a unique
`.claude/lanes/<run>/` directory and durable results under `docs/`.

## 2. Reproduce the current source-guard mismatches without importing the project

Read the actual JSON format in
`tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json`.
For every declared entry, load the exact source bytes using
`git show <commit>:<declared-path>` and compare SHA-256 with the recorded value.
Fail on missing paths, malformed entries, duplicate/unexpected keys or incomplete
enumeration; do not silently skip entries a script cannot decode. At the
original snapshot there were 13 entries and 7 mismatches. Current ae881 has 13
entries and 2 mismatches: authority and session service, listed in G02. The
intermediate c9f one-mismatch result is historical, not current.

Control any ad-hoc comparator with a known unchanged pair that agrees and an
in-memory one-byte change that disagrees. This does not replace sensitivity
testing of the actual proof loader. Review how the gate consumes the bindings,
not just whether a new digest can be computed.

For current worktree bytes, explicitly identify that different measurement; a
comparison of committed blobs cannot certify uncommitted changes.

## 3. Focused validation by gap

Select the actual relevant test IDs after reading their bodies. Use `-n 0` for
single tests and PostgreSQL contention suites. Save complete output and inspect
the process exit code after completion; never infer success from a piped tail.

| Work | Starting test paths / required assertions |
|---|---|
| G01 remaining cuts | Preserve now-received real-clock cases in `tests/unit/web/sessions/test_continuation_action_fit_successor.py`; model-control/readback and actual PostgreSQL handover/recovery suites; add only required remaining cut scenarios |
| G02 proof | `tests/unit/architecture/test_web_landscape_mutation_fencing.py`; relevant required-work recipe/binding controls; CI ordinary/proof partition verification |
| G03 test migration | Named repair-cycle case in `tests/unit/web/composer/test_service.py`; `tests/unit/scripts/test_composer_advisor_binding.py`; relevant live-phase and attribute-contract checks |
| G04 epoch | Three identified cases in `tests/testcontainer/web/test_schema_probe_postgres.py`; preserve current-schema drift/reflection/paid-effect opposition |
| G05 publication | `tests/unit/web/sessions/test_content_publication_legacy_source.py`; current publication/self-revert tests and applicable PG wrappers |
| G06 release docs | `tests/unit/docs/test_release_version_surfaces.py`, `test_readme_release_surface.py`, `test_staging_session_recreation_policy.py`; `tests/unit/website/test_release_site_contract.py`; actual ECS/ACA schema-fact/documentation tests |
| G07 teaching | `tests/unit/web/composer/test_tool_result_variant_teaching.py`, `test_tool_definitions_documented_count.py`; continuation accepted/refusal result contracts |
| O03 preparation failure | `tests/unit/web/sessions/test_continuation_supported_paths.py::test_supported_driver_reaches_original_paid_transitions[read_end_retry]`; original clocks/accounting and bounded production profile; inspect raw R220 evidence described in OPEN-QUESTIONS |

The exact combined O02 acceptance permits actual session/authority APIs; do not
add a final-worker or live-provider requirement beyond the original scenario.
Keep source inspection, execution results and performance measurements distinct.

Do not re-run unrelated broad suites for each small correction. Run every
whole-tree gate whose scanned inputs the change affects. Shared runtime/schema/
lock changes require the full appropriate pre-merge checks after integration,
including the PostgreSQL selection. Failed process/peer-lease tests require
serial diagnosis and base comparison under the repository's known-flake policy;
neither ignoring them nor automatically blaming the new change is evidence.

## 4. Preserve sensitivity and substantive behavior

- A test migrated off a deleted API must still fail for the behavior it was
  intended to prevent; importability alone is insufficient.
- A schema-version correction must preserve drift/refusal checks rather than
  compare the same constant to itself.
- A source-binding update must retain rejection of changed source and malformed
  or missing proof inputs. Do not weaken the exhaustive failure assertion.
- A palette-derived teaching test must detect missing continuation guidance,
  rather than merely increment an expected count.
- A runtime-rollover test must traverse actual worker/authority/store ownership
  and preserve paid-call accounting. O02's bounded cold-restoration scenario has
  the narrower API allowance above. A codec roundtrip or manually inserted row
  alone does not establish runtime rollover.
- PostgreSQL race tests must establish the intended interleaving/lock wait.
  Repeated sequential execution does not prove contention handling.

Use existing mutation/control infrastructure where available. Plant changes only
in an owned isolated tree, restore only your own mutations and never land them.
There is no requirement to create new process ceremony or signed receipts.

## 5. Final integrated-source qualification

Establish test capacity and current host load first; run one broad suite at a
time. Use the repository's canonical full-suite driver with assigned workers,
an explicit root/log directory as needed, and stages matching the current release
requirements. The expected shape is:

```bash
cd <repo-root> && scripts/full-suite-gate.sh --execute --detach --workers <assigned-workers> --stages ruff,mypy,contracts,lints,pytest,testcontainer
```

Read `launcher.exit`, actual `supervisor.exit`, `supervisor.json`, `summary.txt`
and driver/stage receipts. Require completed cleanup and passing frozen-source
evidence; `.done` alone and terminal output alone are insufficient. Follow the
current driver's interpretation of keyless lint results and the release A4
finding-set comparison; do not equate the known nonzero lint corpus with a
mandatory zero-findings target or accept malformed scanner output.

The ordinary selection excludes PostgreSQL testcontainers. The required serial
selection is `pytest tests/ -m testcontainer -n 0`, run through the appropriate
logging/gate mechanism. Python checks do not establish frontend/browser/gateway
results; obtain the actual required CI or local results bound to final source.

Trust-tier work is keyless only: scan base and tip with the explicit rule/root
selection and compare added/removed finding sets. Do not run suspended signing,
rekeying or fingerprint-baseline regeneration, and do not access the operator key.

After authorized integration, verify the actual receiving commit and relevant CI
checks. Qualification belongs to that exact source, not to a donor branch,
historical report, moving dirty tree or this package. Do not push, merge, deploy
or reset a store solely because a validation checklist mentions the action.

## 6. Completion report

For G01–G07 and each open item taken on, record corrected/already-fixed/decision-
needed/unresolved with a short explanation, code/test pointers and actual
validation exits. Keep executed results separate from inspected assertions and
unrun checks. State final branch/SHA and remaining risks without turning missing
review work into alleged broken code. A concise updated disposition table is
sufficient; no signatures, sealed plan manifests or receipt sidecars are needed.
