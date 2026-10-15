# Affected LLM examples refresh

Completed the twelve affected configurations in seven serial local launcher
groups on immutable commit `b7521db800fcb225de00f0d6de5cc35d48437e7e`.
Six launchers exited 0; the fault-enabled sentiment launcher exited 1 with a
verified PARTIAL outcome. Total elapsed time was 275.975 seconds, within the
630-second envelope; subprocess time totaled 163.679 seconds. Every source
clock remained frozen and each case's original inputs remained unchanged.

| Portable command from fresh repository copy | Configs | Exit | Seconds |
|---|---:|---:|---:|
| `bash examples/ab_llm_experiment/run.sh` | `settings.yaml`, `settings_models.yaml`, `settings_arm_loss.yaml` | 0 | 22.747 |
| `bash examples/document_review_panel/run.sh` | `settings.yaml`, `settings_incomplete.yaml`, `settings_run_as_row.yaml` | 0 | 20.892 |
| `bash examples/reference_join_fork_llm/run.sh` | `settings.yaml` | 0 | 7.675 |
| `bash examples/chaosllm_sentiment/run.sh` | `settings.yaml` | 1, PARTIAL | 7.525 |
| `WORKERS=1 bash examples/multi_worker/run.sh` | `settings.yaml` | 0 | 66.808 |
| `bash examples/join_refused/run.sh` | `settings.yaml`, `settings_mismatched.yaml` | 0 | 27.353 |
| Private `local_fixture.py rate_limited_llm` helper | `settings.yaml` | 0 | 10.679 |

The sentiment fixture intentionally injects malformed responses. Two audited
local HTTP 200 responses had empty bodies, matching its committed
`empty_body_pct: 1.0` fault setting. Both produced nonretryable JSON decoding
errors and routed their original rows to quarantine: ten source rows were
conserved as eight successes and two failures. This expected fault-path result
was retained without retrying for exit 0. The initial harness expectation of
exit 0 and its stopped receipt remain in the private evidence.

The other launchers passed their shipped checks, including intentional
internal PARTIAL cases. Independent audit queries measured 120 completed
terminal rows shared equally between leader and follower in `multi_worker`.
`join_refused` admitted the matching follower, refused the mismatched config
for the asserted config-hash reason and retained exactly two registered
workers. The rate-limited pipeline completed all eight source rows.

The existing private Python 3.13.15 environment imported both `elspeth` and
`elspeth_lints` from the immutable export through explicit source roots. LLM
module raw SHA256 was
`774c2de7c5489f23cca132b1f60396ca091074d7472a1ffe3729be550eb6d83f`.
Declared and independently computed canonical identities matched
`sha256:705f5c0d4bb975a2`; all eleven resulting audit databases persisted that
pin. The twelfth configuration is the deliberately refused join arm and
creates no separate run/database. The full exported input clock was
`34175f4ae783f44281c16e74b9c12a75bfa210a0e6c74f5e1c37a6c5e5cdd534`.

The [two new chain examples](release086-chain-examples-20261015.md) were
bridged without repetition: all 1,908 runtime source paths/bytes and their
shipped inputs/index entries matched this commit. The original
[81-example qualification](release086-examples-validation-20261015.md)
remains unchanged and historical. This refresh covers the twelve affected
LLM configurations, not a rerun of that campaign or whole-release clearance.

Only owned loopback mock servers were used, serially, with at most two engine
workers and one worker per mock endpoint. No dependencies were installed or
synchronized; no real provider credentials, paid calls, external network,
broad/default/native/PostgreSQL suites or source changes were involved.
Detailed commands, exits, clocks, audit queries, payloads and preserved cases
are under `.claude/lanes/release086-examples-20261015/refresh-b7521db800/`:
`REPORT.md`, `summary.json`, `results.jsonl`, `source-clock.json`,
`llm-audit-pin-evidence.json`, `sentiment-fault-evidence.json`,
`terminal-metrics.json`, `new-chain-bridge.json` and `logs/`.
