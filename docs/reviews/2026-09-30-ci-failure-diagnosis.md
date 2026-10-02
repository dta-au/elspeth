# CI failure diagnosis — 2026-09-30

CI has several independent failures. Splitting the Python suite into smaller
jobs is recommended, alongside explicit resource cleanup and repairs to the
timing-sensitive tests. A rerun alone cannot resolve all the observed blockers.

This is a diagnosis and proposed repair scope; no workflow or production code
was changed during this investigation.

## Scope and current source

Live GitHub API results at investigation time:

```text
main:          d4c5da4b6295c712bb3c53a011f2ee4d89e66841
release/0.8.1: 03b990d64567f84fb332d2a37d76860158ffa7d0
local HEAD:    a2f0281ff09cb5a3caa997660c3b4fe8306fc675
git rev-list --count origin/release/0.8.1..HEAD: 22
```

The local remote-tracking reference matched the live release SHA. The latest
release CI runs tested `03b990d6`, not the newer local changes. The workflow,
root test fixtures, template worker failure tests, and recovery harness have
no changes between that release SHA and local HEAD.

## Latest release failures

| Run | Job | Terminal result | Evidence |
| --- | --- | --- | --- |
| [Release push 36526535297](https://github.com/dta-au/elspeth/actions/runs/36526535297) | Python 3.13 | Exit 3 | `OSError: [Errno 24] Too many open files` inside pytest/Hypothesis reporting; `108 failed, 20949 passed, 43 skipped, 1 xfailed, 1661 errors` |
| Same push | Python 3.12 | Success | GitHub job conclusion `success` |
| [Release PR 36526541956](https://github.com/dta-au/elspeth/actions/runs/36526541956) | Python 3.12 | Exit 1 | `4 failed, 59131 passed, 190 skipped, 1 xfailed` |
| Same PR | Python 3.13 | Exit 1 | `1 failed, 59242 passed, 81 skipped, 2 xfailed`; worker crash and total coverage `71.67%`, below `85%` |

Static analysis, PostgreSQL testcontainers, host-only tests, both frontend
jobs, dependency/license audit, state-engine validation, and Bicep validation
all succeeded in both latest release runs. The `CI Success` failure is the
aggregate reporting those Python failures.

### File-descriptor exhaustion

The push's first visible failure was the `noncanonical` case of
`test_a_crash_inside_the_verdict_leaves_no_verdict_and_the_group_is_re_flushed`.
After that, many setup errors occurred on the same worker before pytest itself
failed opening `hypothesis/extra/_patching.py` with `Errno 24`.

The original test traceback was obscured by the reporting crash. The log proves
descriptor exhaustion; it does not identify which preceding test accumulated
the descriptors or the CI process's descriptor limit.

A bounded local experiment identified a relevant cleanup defect:
`tests/integration/pipeline/test_barrier_hold_payload.py::build_pipeline`
constructs a file-backed `LandscapeDB` and returns it to callers without
establishing teardown. Its callers include collector and recovery tests.

```text
100 helper constructions, normal GC:   FDs 5 -> sampled peak 41 -> 38
explicit gc.collect() afterward:       FDs 5
100 constructions, GC disabled:        FDs 5 -> 305
100 constructions, retained env refs:  FDs 5 -> 305
explicit env['db'].close() each time,
with GC disabled:                      FDs 5 throughout
```

These positive and negative controls establish that the helper's cleanup
depends on garbage collection, with three SQLite descriptors per retained
database. They do not prove this helper alone caused the CI exhaustion.
The local descriptor limit was `1048576`, which can conceal accumulation that
fails under a smaller limit.

The collector verdict file completed locally with 19 passing tests, exit 0.
A bounded audit-export/batch-recovery selection completed with 67 passing
tests, exit 0. Instrumentation with explicit GC returned descriptors to the
baseline. These checks bound the diagnosis; they are not a full-suite result.

### Python 3.12 timing and template failures

The complete PR log reports:

1. Aggregation process-death test, `transform-second_continuation_leased`:
   `child did not reach process seam second_continuation_leased within 15.000s`.
2. Hosted-leader deployment-profile test:
   `child did not reach process seam profile-resume-completed within 15.000s`.
3. Template mid-render stop test, SIGINT:
   expected `{'result': 'done'}`, received a template execution-time-limit error.
4. The same template test, SIGTERM:
   expected `{'result': 'done'}`, received a template CPU-limit error.

The template test performs a fixed 40,000-by-300 nested loop, sleeps 0.3 seconds,
sends a stop signal, and assumes the render completes within the normal
template limits. These tests couple their assertions to elapsed time or
machine speed. The errors do not prove that graceful stop or recovery semantics
are incorrect. Repair requires controlled scheduling/resource conditions,
while retaining separate tests of real timeouts and CPU limits.

### Python 3.13 worker loss and coverage

The PR log reports:

```text
[gw1] node down: Not properly terminated
worker 'gw1' crashed while running
tests/unit/web/composer/test_compose_loop_wire_decode.py::TestWireFactParity::test_invocation_and_p4_row_carry_the_same_facts[non-object-none]
FAIL Required test coverage of 85% not reached. Total coverage: 71.67%
```

There is no reported signal, exit code, or OOM message for this worker. Earlier
PR runs do contain exit 137 and runner-shutdown messages, but those do not
establish the cause of this latest crash. Coverage loss following worker death
is plausible; the recorded coverage result still requires independent
verification after a complete run with all workers accounted for.

The release workflow already reduced workers to four on Python 3.12 and two
on Python 3.13. That reduction did not eliminate these failures.

## Dependency PR failures on main

[aiohttp PR CI 36605024612](https://github.com/dta-au/elspeth/actions/runs/36605024612),
[anyio PR CI 36604982202](https://github.com/dta-au/elspeth/actions/runs/36604982202),
and [cryptography PR CI 36604953332](https://github.com/dta-au/elspeth/actions/runs/36604953332)
all failed the same job categories: both Python jobs, trust-tier static
analysis, and browser E2E. These PRs target `main`, whose workflow and code
are different from the release branch.

The aiohttp static log explicitly reports ten expired tier-model allowlist
entries in `config/cicd/enforce_tier_model/contracts.yaml`. Inspected entries
include expiry `2026-09-15`. These are actual expiry errors, not evidence that
the dependency bump broke the linted code.

Its browser log reports `9 failed, 10 skipped, 106 passed`; all nine failures
are Composer workspace screenshot comparisons. The downloaded populated
freeform comparison localizes the visible difference to the Checks badge and
adjacent Run tab. The precise rendering cause has not been established.

Sharding Python tests will not repair either of these independent blockers.

## Recommended smaller-job design

Split both Python-version lanes into stable, bounded test groups, then use one
coverage aggregation job for Python 3.13:

- Separate expensive process-death/recovery tests from ordinary tests.
- Partition the remaining unit, integration, property, invariant, and example
  tests using measured durations; directory names alone do not ensure balance.
- Derive assignments from pytest collection with the existing marker selection.
  Verify that the union exactly matches the selected suite and that no node
  appears twice. Preserve all existing deselections.
- Keep per-job worker counts small. Bound simultaneous jobs with matrix
  `max-parallel`, especially on the shared self-hosted runner.
- Preserve the serial PostgreSQL job and the existing frontend/static jobs.
- Upload complete logs, JUnit, durations, raw coverage data, and resource
  diagnostics from each job, including failing jobs.
- Require each job to finish successfully and provide its expected coverage
  artifact. Aggregate only Python 3.13 branch coverage, normalize checkout paths,
  and enforce the existing global and subsystem floors after combining.
- Make `CI Success` require every shard and the coverage aggregation result.

[Coverage.py supports combining parallel data and path normalization](https://coverage.readthedocs.io/en/latest/commands/cmd_combine.html).
[GitHub Actions supports matrix concurrency limits and failure controls](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations).
Use `fail-fast: false` to retain failure evidence from every shard.

Pair this change with explicit test-owned database teardown and diagnostic
measurements of descriptors, memory, limits, and worker termination. Shorter
worker lifetimes limit accumulation and reduce rerun cost; explicit cleanup
addresses the ownership defect. Do not lower coverage floors or weaken runtime
sandbox limits to accommodate the test harness.

## Evidence retrieval caveat

`gh run view --job ... --log` yielded incomplete logs for large test jobs,
ending around 59–60% with no terminal result. Reading the raw job endpoint
recovered the complete summaries and exit codes:

```bash
gh api --allow-escape-sequences \
  repos/dta-au/elspeth/actions/jobs/JOB_ID/logs > job.log
```

The latest PR job IDs are `109270727111` (Python 3.12) and `109270727195`
(Python 3.13). The failing push Python 3.13 job is `109270707022`.

No broad local suite was launched, no existing edits were altered, and no
commit, push, workflow rerun, signing operation, or issue mutation was performed.

## Subsequent operator update

The operator reported cleaning the runners after finding root-owned files
blocking their operation. This establishes an observed runner ownership problem;
it does not by itself attribute every earlier test failure to that problem.
It is compatible with the historical coverage database-open failure, whose
precise mechanism remains unproven from the logs.

A subsequent live check still found remote `release/0.8.1` at `03b990d6` and no
new release CI run since the previously inspected runs. The local branch had
advanced to `49c184508` and included merge `b44606437`: test and integration
containers now request `--ulimit nofile=65536:524288`, and build-push jobs use
per-run checkout paths. These local changes have not yet been tested by the
remote runs discussed above.

The next discriminating evidence is a complete run on cleaned runners with
the intended workflow version. Keep ownership cleanup, descriptor limits,
explicit test teardown, and suite sharding as separate changes when assessing
which failures remain. Sharding remains useful for bounded resource lifetimes
and faster reruns, but its necessity as a remedy for runner contamination
should be reassessed after that run.

## New runs on 49c184508

The next observed release runs tested `49c1845085d36811b120ef1c540048463e32aabc`:
[push 36630347813](https://github.com/dta-au/elspeth/actions/runs/36630347813)
and [PR 36630357860](https://github.com/dta-au/elspeth/actions/runs/36630357860).
The push completed; the PR was still running at the latest inspection.

The push Python 3.13 tests completed successfully:

```text
59383 passed, 81 skipped, 2 xfailed in 6823.36s
Required test coverage of 85% reached. Total coverage: 87.03%
```

Its subsequent subsystem coverage checks failed: Landscape reported 88%
against a 92% floor, and orchestrator reported 89% against a 90% floor.
Canonical and contracts cleared their respective floors. This is a completed
coverage result, distinct from the earlier worker-loss result.

The dependency audit failed on both runs. The supplied audit log reports:

```text
oauthlib 3.3.1  CVE-2026-49265   Fix Versions: 4.0.0
pyjwt    2.13.0 CVE-2026-102274  Fix Versions: 2.14.0
```

Both vulnerable versions are still in the local `uv.lock`. The lockfile's
dependency graph places oauthlib beneath requests-oauthlib, used by kubernetes
and msrest; PyJWT is also used by mcp and msal, as well as ELSPETH authentication.
Upgrade resolution and compatibility checks are needed; no new advisory ignore
was added and no lockfile was changed during diagnosis.

The PR Python 3.12 job failed on the two mid-render stop cases, SIGINT and
SIGTERM. Both received `TemplateError('Template exceeded the CPU limit')`.
The supplied terminal summary is:

```text
2 failed, 59273 passed, 190 skipped, 1 xfailed in 4723.33s
Process completed with exit code 1
```

The test executes a 40,000-by-300 nested loop and sleeps 0.3 seconds before
sending the signal. Production `_serve_request` sets the soft cumulative CPU
limit to `ceil(current_cpu_usage + 2)` and renders by iterating generated
pieces. The test's success therefore depends on that fixed workload fitting
within roughly two to three additional CPU seconds, and the sleep does not
prove the signal arrived during rendering. The appropriate repair is a
synchronized test render with an explicit midpoint and bounded release, while
retaining independent real CPU-limit tests and the production limit.

A local focused invocation of those same two tests completed with exit 0,
`2 passed in 12.98s`. That isolated pass does not resolve or explain away the
CI failures. No production or test implementation was changed.
