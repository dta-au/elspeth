# Independent review follow-up sweep — 0.8.1

Status: implementation and integration in progress; final gates pending.

Target: `release/0.8.1`, inspected at `ee96258caa39a0f5038b4b7576a197bd57d14d06`.
Delivery branch: `fix/review-sweep-20260925`, in
`.claude/worktrees/review-sweep-20260925`. This report follows `sweep-prompt.md`
and the complete `fix-review-consolidated.md`, including its re-review at
`1f500c6ae`. The sweep stops at ready for review: no release merge or push.

## Prior and in-flight work

The release log requested by the brief returned:

```text
$ git log --oneline 1f500c6ae..release/0.8.1
ee96258ca Merge examples/replay-verify-20260924: replay_verify walkthrough into release/0.8.1
e5109f49e examples: assert the fixed replay/verify behaviour in replay_verify
84b4ab1da examples: date the replay plugin-inventory claim in replay_verify README
20e0250a8 examples: add replay_verify walkthrough for run_mode live/replay/verify
```

The original six fixes and the closed re-review corrections are already in
this base. The example is already merged. The remaining sweep items were
still present in the inspected source; they were not covered by the following
related work:

| In-flight branch | Inspected head | Finding |
|---|---|---|
| `fix/replay-completion-20260925` | `11e037eaf` | Two release-exclusive commits repair source failures/order and sink diversions. The CLI, verdict export and comparison-policy targets were byte-identical to release. Its PostgreSQL gate was confirmed live by PID, command and cwd during inventory. |
| `fix/replay-outcomes-20260925` | `1f500c6ae` plus dirty files | Earlier work incorporated into replay-completion; left untouched. |
| `fix/5887-rebased` | `2c41e123e` plus dirty files | Output declaration/routing work. All six implicated group-key plugins and their helper were byte-identical to release. Left untouched. |
| Older `fix/k056-*` lanes | Reviewed individually | Some unmerged commit objects have patch-equivalent integrated changes. Ancestry alone does not establish missing behavior. No wholesale cherry-pick or cleanup was performed. |

Detailed live-state inventory: `.claude/lanes/sweep-20260925/inflight-review.md`.
Other lanes' test results were not used as this sweep's verification.

## Item evidence

Pending final integration table and current file-line measurements.

F8 and K103-L1 are committed as `8b4aa1398`. The focused CLI command covered
`test_run_mode_admission.py`, `test_web_command.py` and
`test_validate_command.py`: **exit 0, 93 passed**
(`/tmp/sweep-cli-green-final.log`). Independent final-tests-only mutation
against the original CLI produced **exit 1, 7 failed, 4 passed**; the same
selection on the fixed source produced **exit 0, 11 passed**
(`/tmp/sweep-cli-review-mutant.log`, `/tmp/sweep-cli-review-final-green.log`).

F11 is intentional virtual audit evidence, resolved by documentation rather
than deletion or vocabulary change. The normal effect coordinator reserves
`sink_write`, while `VirtualReplaySinkEffect` supplies `NO_PUBLICATION` and
virtual evidence; calling its publication methods raises an invariant error.
The effects retain `publication_performed=false`. There is no runtime fix to
revert for this documentation resolution.

## Gate evidence

Unchanged-base static gate:
`/tmp/sweep-20260925-base-static/20260924T170003Z-sweep-base-20260925-3388343/summary.txt`.

```text
stage=ruff exit=0 seconds=1 fatal=1
stage=mypy exit=0 seconds=41 fatal=1
stage=contracts exit=0 seconds=25 fatal=1
stage=lints exit=1 seconds=168 fatal=0 findings=2355
after=ee96258caa39a0f5038b4b7576a197bd57d14d06 01ba4719c80b6fe9 frozen=yes
RESULT=PASS
```

The lint exit is the existing fail-closed corpus, not a clean lint result.
Final all-stage gate and normalized finding-set comparison remain pending.
