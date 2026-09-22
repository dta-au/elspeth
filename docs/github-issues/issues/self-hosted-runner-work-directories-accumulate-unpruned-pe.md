---
title: Self-hosted runner work directories accumulate per-run checkouts that nothing ever prunes
labels: [area/deployment, type/bug]
---

Every CI job checks out into a directory unique to its run, and nothing removes it afterwards. Measured on 2026-09-05 across four self-hosted runners: 114 accumulated checkout directories totalling 120 GB, growing by roughly 6 GB per CI run.

## Mechanism

Each job sets a per-run checkout path and hands it to `actions/checkout`:

```yaml
env:
  CI_CHECKOUT_PATH: ci-${{ github.run_id }}-${{ github.run_attempt }}-<job>
# …
- uses: actions/checkout
  with:
    path: ${{ env.CI_CHECKOUT_PATH }}
```

Six jobs in `.github/workflows/ci.yaml` do this (`static-analysis`, `test`, `integration`, `supply-chain-audit`, `e2e-frontend`, `frontend-unit`), and `.github/workflows/enforce-allowlist-judge-gates.yaml` uses the same pattern for two more (`judge-quality`, `override-rate`). Nothing in either workflow, and nothing in the runner configuration, removes the directory when the job ends.

The per-run path was introduced deliberately, to stop stale shared workspaces breaking checkout. It fixed that and created unbounded growth: a unique path per run is never reused, so it is also never reclaimed.

## Measured on disk, 2026-09-05

| runner | `_work` total | accumulated `ci-*` directories |
|---|---|---|
| 1 | 32 GB | 33 |
| 2 | 30 GB | 29 |
| 3 | 31 GB | 31 |
| 4 | 27 GB | 21 |
| **total** | **120 GB** | **114** |

Per-directory sizes observed: static analysis and the two test jobs about 1.1–1.2 GB each, e2e-frontend 1.4–1.5 GB, judge-quality 649 MB, override-rate 542 MB, frontend-unit 514 MB. Each is a full repository tree plus its virtualenv and uv cache.

Rate: the six `ci.yaml` jobs take a checkout on every run, so a single CI run leaves roughly 6 GB spread across the pool. The two judge-gate jobs accumulate on their own triggers — `override-rate` on configured pull requests and pushes, `judge-quality` on trusted pushes only — so they add to the total without being part of every CI run, which is why their directories appear in the table. There were 28 runs on 5 September alone. The filesystem at the time of measurement: 1.7 TB total, 1.2 TB used, 463 GB available, 72% full. The 120 GB already accumulated is about a tenth of everything used on the host, and at the observed run rate the remaining headroom is weeks rather than months.

## Impact beyond disk

The same host runs the local test suites and full-suite runs. Disk pressure and page-cache eviction on a box measured at load 41 during concurrent CI may be upstream of some of the timing failures under investigation. That link is **not** claimed as measured — it is a reason to fix this rather than defer it.

## Fix

- **(a)** A cleanup step at the end of each job, guarded with `if: always()`, removing its own `CI_CHECKOUT_PATH`. Simple, per-job, and cannot orphan a directory a concurrent run is using.
- **(b)** A scheduled prune of `ci-*` directories older than N days across every runner's `_work` tree. This catches directories orphaned by cancelled runs, which (a) never will, because a cancelled job may not reach its cleanup step. In the 1–4 September window, 24 runs were cancelled, so orphans from cancellation are the common case here rather than the exception.
- **(c)** Both: (a) for the steady state, (b) as the backstop.

Reclaiming the existing directories is worth about 120 GB, but they must not be bulk-deleted without first confirming no run is in flight.
