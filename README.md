# 0.8.14 integrator lane archive

This is an orphan archive branch. It has no `.github/` directory and no PR, so
it cannot trigger CI. Do not merge it. It was packed on 2026-11-15, before the
integrator session moved to new hardware.

## State at pack time

- `claude/zen-turing-pphown` (PR dta-au/elspeth#368) is at `01f39577`: every
  session #10 lane through `b02c94dc` plus its 0.8.14 CHANGELOG entries.
- **No pushes to #368 until the 0.8.13 parent lands and the maintainer says
  go.** Each push takes both self-hosted runners away from 0.8.13.
- Session #10 (`claude/compassionate-brahmagupta-o0nhlw`) holds the box for its
  p5 run. It will send CHANGELOG/COMPAT notes per lane and an explicit
  HANDOVER. After that, merge each lane with `--no-ff`, add CHANGELOG entries
  and full-gate locally (`--workers 4`). Push only on the maintainer's go-ahead.
- Final full gate on `d00fbfe6`: ruff, mypy and contracts 0. Pytest 75547
  passed, 24 failed: 21 known sandbox gaps, plus 3 load flakes that pass
  serially (22/22). Its logs are in `gate-runs.tar.gz`, under
  `root/20261114T143521Z-push-tip-375`.

## Contents

| File | What |
|---|---|
| `lanes-push-tip.tar.gz` | `.claude/lanes/` from the push-tip integration worktree |
| `lanes-integrate-0814.tar.gz` | `.claude/lanes/` from the integrate-0814 worktree |
| `lanes-py314-on-0813.tar.gz` | `.claude/lanes/` from the Python 3.14 worktree |
| `gate-runs.tar.gz` | `/tmp/elspeth-gates`, the full-suite-gate run dirs (summary.txt, per-stage logs) |
| `scratchpad.tar.gz` | session scratchpad: the 4 CI workflow patches (`ci-patches/`), `review-365.md`, focused-check logs |
| `integrate-0.8.14.bundle` | the unpushed local branch `integrate-0.8.14` (`f55f1cb5`) |
| `python-3.14-on-0.8.13.bundle` | the unpushed local branch `python-3.14-on-0.8.13` (`58c27c19`) |

Neither bundled branch is an ancestor of `01f39577`. Their prerequisites are
commits on origin. Restore one with:

```bash
git fetch origin
git fetch ./integrate-0.8.14.bundle integrate-0.8.14:integrate-0.8.14
```

## Open maintainer decisions

- Guardrail retries re-bill fields that already passed; plugin version bump.
- Model output from pipeline `llm` nodes is still recorded VERBATIM (the #365
  follow-up). Should it get its own issue?
- Dataverse: make the 300 s throttle budget configurable? Retry
  500/502/504/timeouts?
- Textract verify/replay questions (see CHANGELOG Known limits).
- Power Automate #131; `truncate` #146 (in session #10's p5); planner 64-byte
  allowance (removed in p5); #123 (in p5); #27 and #83 (asked by session #10).
- Two masquerade baseline entries to review. Tier-model allowlist re-signs:
  operator only, and lint signing is off for now.
- Apply the 4 workflow patches (`ci-patches/`). One fixes the live-provider
  matrix, which is missing `azure_content_understanding`; that test fails on
  base `21e74acc`.
- The PostgreSQL testcontainer suite has not been run.
- Close PR #369 as superseded (left to the maintainer).
