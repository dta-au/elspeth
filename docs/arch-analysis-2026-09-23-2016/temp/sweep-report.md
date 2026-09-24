# Independent review follow-up sweep — 0.8.1

**Ready for review.** The final six-stage gate records `RESULT=PASS` and
`frozen=yes`; the deliberate lint-corpus exception is documented below.
The sweep has not been merged into release or pushed. This report follows all ten
items in `sweep-prompt.md` and the complete `fix-review-consolidated.md`, including
the re-review at `1f500c6ae`.

Delivery branch: `fix/review-sweep-20260925`.
Production implementation head: `79ed2c3e577eaca7a21ccae553b91d00be1e3a21`.
Repaired verification head: `2f1507616cc33bbb4dca586fc822ab959896876f`.
Worktree: `.claude/worktrees/review-sweep-20260925`.
Integrated source baseline: `11e037eaf015fc3b3bd07b54f4084216e9df59f4`, brought in
by merge `b71d695ec` after the sweep began at `ee96258ca`.

## Prior implementation and concurrent work

The requested initial `git log --oneline 1f500c6ae..release/0.8.1` showed the example
commits and merge `ee96258ca`. The previously reviewed K056 F1–F7/F9 corrections
(`58582f847`), K062 R1–R4 corrections (`313a85bb1`, `3927761a9`), accepted R5
retry-test disposition, and ADR-019 correction (`79dfb3b1d`) were already present.
Their commit ancestry was checked again against the frozen candidate: every
`git merge-base --is-ancestor <commit> HEAD` returned **0**. These accepted prior
review conclusions were retained; no duplicate implementation was made.

The initial live inventory established that the replay-completion lane repaired
source failures/order and sink diversions, while its CLI, export, and comparison
policy targets were unchanged. Its two commits, `4ee0e9d7b` and `11e037eaf`,
subsequently landed on release and were merged into this review branch. The dirty
older replay-outcomes lane was left untouched. The related batch-row lane did not
change any of the six implicated key-extraction plugins or their shared helper;
this sweep therefore implemented K063-L1 without copying unrelated work. Older
K056 branches included patch-equivalent changes: unmatched ancestry alone was not
treated as missing behavior. Details: `.claude/lanes/sweep-20260925/inflight-review.md`.

During gate execution release first advanced to `6c3da5400`. A live
`git diff --name-only 11e037eaf..release/0.8.1` returned only:

```text
docs/plans/2026-09-23-composer-strict-tool-contracts.md
docs/plans/2026-09-25-composer-strict-contracts-s2-plan.md
```

Release later advanced to `d479eb2b4` (`chore: retire local tracker and code index
for shared GitHub workflow`) and then `4403c8e68`, which includes the separately
reviewed Scenario C gateway bounds, expanded-token replay identity, source-parent
ambiguity refusal, and additional source diversion/discard controls. At final
verification, release is `ea5fa50d588306a4b22a671ffadf7b45bb0b07c7`, also containing
the separate template worker/block-scope repair `80172ad20` and its peer-lease
inventory update. Those
concurrent changes are not included in this sweep's source baseline. The review
branch remains based on `11e037eaf`; this report does not certify its combination
with those later release changes. The frozen candidate was preserved.
`git merge-base --is-ancestor 2f1507616
release/0.8.1` returned **1**: the new sweep remains review-branch work. No remote
or deployed-state claim is made.

## Item disposition and integrated commits

All file locations below were read from `79ed2c3e5`; all commits in this table were
independently confirmed ancestors of that head.

| Item | Status | Integrated commit and current source evidence |
|---|---|---|
| F8 — validate source-run admission | Fixed | `8b4aa1398`; `src/elspeth/cli.py:2016` reuses run's raw and parsed admission. `:572` handles an absent SQLite file before opening it. Both commands report the same missing-source message and avoid output/DB creation. |
| F10 — verdict export and visibility | Fixed | `30632c651`, `53360d9eb`, docs `8b1755f75`; `src/elspeth/core/landscape/exporter.py:984` emits verdicts, `src/elspeth/core/landscape/verification_reads.py:176` pages validated joined reads on the export connection, `src/elspeth/contracts/audit_export.py:34` requires serialization v3. `src/elspeth/cli.py:1466`, `src/elspeth/mcp/server.py:300`, and `src/elspeth/tui/widgets/node_detail.py:306` expose recorded verdicts. |
| F11 — apparent sink writes | Resolved by documentation of valid audit evidence | `e062ac52a`; `docs/reference/configuration.md:130`. The operation is intentional; evidence below explains why it remains. No runtime vocabulary change. |
| NEW-1 — fixed comparison exclusions | Fixed | `e062ac52a`; `src/elspeth/engine/orchestrator/call_mode_session.py:719` reports the exact policy; `docs/reference/configuration.md:112` documents it. No field was added to the ignored list. |
| NEW-2 — template recursion | Fixed | `824c39e54`; `src/elspeth/plugins/infrastructure/templates.py:399` converts parser recursion to TemplateError; `:426` checks depth iteratively before recursive analysis. YAML import now returns HTTP 200 with `is_valid=false`, instead of 500. |
| K063-L1 — non-scalar batch keys | Fixed | `f0322045c`; `src/elspeth/plugins/transforms/_batch_row_types.py:53` provides one value-free scalar-key guard. All six named plugins use it for group/cohort/variant/pair arms; existing batch_stats also uses it. |
| K123-L1 — gateway accepted-name parity | Fixed, including independent-review correction | `48a6cab0e`, `7ce4bf48c`; `tests/unit/deployment/test_aws_ecs_terraform_package.py:1053` captures every gateway-prefixed name, including the bare prefix, and `:1078` asserts supplied names are in the loader's KNOWN_ENV. |
| K051-L1 — required audit attempts | Fixed | `c3b480d5c`; required keyword-only arguments at `src/elspeth/engine/executors/gate.py:273`, `src/elspeth/engine/executors/transform.py:693`, traversal/processor delegates, and `src/elspeth/engine/scheduler_drain.py:261`. Existing nonzero recovery offsets are preserved. |
| K103-L1 — duplicated auth default | Fixed | `8b4aa1398`; `src/elspeth/cli.py:5065` preserves supplied choices and leaves the variable absent when neither flag nor environment specifies one; settings own the default. |
| Example | Updated and executed | `79ed2c3e5`; `examples/replay_verify/run.sh:210` asserts explain/export verdicts, `:233` checks virtual publication evidence, and `:254` confirms validate refuses missing sources. README limitations at `:203` now retain only fixed-field comparison behavior and quoted all-digit IDs. |

K037's matching **recursion symptom** was also repaired in the same small shape
(`824c39e54`, `src/elspeth/core/expression_parser.py:860` and `:868`). The broader
expression resource-budget issue remains deferred: input/literal/output budgets
and Python's much larger parser-stack MemoryError are not claimed fixed.

The consolidated review's NEW-3 remains its accepted tradeoff: template callers
wait for worker capacity, so bursts increase queueing latency. This sweep's AST
depth admission does not change that worker-slot policy.

F10 exports True/False/None decisions, current/source run and call identities,
exact persisted difference JSON, and timestamps. Signed record chains and manifest
counts include them. JSON/CSV derivation, actual CSV file preparation, strict
version refusal, corruption, and joined keyset pagination are tested. The new MCP
query is read-only. CLI text/JSON is token-lineage scoped; the interactive Run
summary and run-level MCP query also show operation-parented decisions. Invalid
JSON, invalid stored booleans, missing/wrong call parents, mode/source mismatches,
and cross-run ownership corruption raise AuditIntegrityError. Outer joins retain
invalid references for rejection rather than silently omitting them.

F11 evidence was inspected before deciding the disposition:

- `src/elspeth/core/landscape/execution/sink_effect_reservation.py:933` reserves a
  durable `sink_write` operation linked to the effect.
- `src/elspeth/engine/executors/replay_sink_effect.py:154` records virtual evidence,
  `:162` uses NO_PUBLICATION, and `:176` refuses actual publication.
- `src/elspeth/engine/executors/sink_effects.py:574` finalizes that no-publication
  effect without invoking the configured sink's publication methods.
- Existing file/SQL/cloud tests, strengthened with operation/effect assertions,
  prove unchanged targets and `publication_performed=false`.

The evidence selection is
`tests/unit/engine/test_replay_sink_effect.py::test_non_live_run_with_real_sink_records_virtual_effect_without_touching_target`;
it is included in the 47-pass `L/policy-final-green.log` selection below.

Deleting that operation would delete audit evidence. There is no runtime fix to
revert for this documentation resolution, so no artificial red result is claimed.

## Red-green and independent verification

Test commands ran from the relevant isolated candidate with
`PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest`.
Both import roots were checked. Tests used `-n 0` or at most `-n 2`, wrote logs,
and the responsible lane observed process completion and exit codes. The results
below were checked against the actual logs; they are not substituted for the final
combined gate. `L/` below means `.claude/lanes/sweep-20260925/`.

| Item / test command selection | Red evidence | Green evidence |
|---|---|---|
| F8/K103: `test_run_mode_admission.py::test_missing_source_admission_matches_for_run_and_validate`, `::test_validate_accepts_completed_source_without_writing_output`, `test_web_command.py::TestWebCommandAuthBridging::test_default_auth_is_owned_by_settings` under `tests/unit/cli/`, `-n 0` | Final tests against original CLI: exit 1, **7 failed, 4 passed**; `/tmp/sweep-cli-review-mutant.log`. Passing controls distinguish existing supported behavior. | Same selection: exit 0, **11 passed**, `/tmp/sweep-cli-review-final-green.log`; affected CLI suite **93 passed**, `/tmp/sweep-cli-green-final.log`. |
| F10 export: `tests/unit/core/landscape/test_verification_export.py -n 0`; `test_export_contains_exact_verification_evidence`, `test_serialization_contract_rejects_previous_version` | Exit1, **3 failed**: empty verdict arrays for JSON/CSV and old v2 identity; `/tmp/f10-export-red.log`. | Signed/unsigned JSON/CSV plus corrupt-verdict rejection and reader matrix: exit 0, **60 passed**, `/tmp/f10-reader-final.log`. Actual call_verification.csv test: exit 0, **1 passed**, `/tmp/f10-csv-verdict.log`. |
| F10 surfaces: `tests/unit/core/landscape/repository_integration/test_verification_visibility.py -n 0`; `test_read_only_mcp_tool_returns_persisted_verdicts`, `test_explain_cli_shows_persisted_verdict`, `test_explain_tui_run_summary_shows_persisted_verdict`. Historical logs use its former `tests/integration/audit/` location. | Missing CLI/MCP fields/tools: **8 failed**, `/tmp/explain-mcp-red.log`; missing TUI rendering: **4 failed, 10 passed**, `/tmp/explain-mcp-tui-red.log`, both exit 1. | Bounded combined exporter/reader/surface checks: exit 0, **175 passed**, `/tmp/f10-bounded-final.log`. Independent integrated F10 tests: exit 0, **147 passed**, `L/f10-independent-final-tests.log`. |
| F10 integrity: `tests/unit/core/landscape/test_call_mode_persistence.py -n 0`; `test_verification_read_rejects_persisted_corruption` and positive tri-state controls | **26 failed**, `/tmp/verification-reads-red.log`; Text/BLOB control **2 failed**, `/tmp/verification-reads-blob-red.log`, exit 1. | Final reader plus bound manifest negative test: exit 0, **55 passed**, `/tmp/f10-binding-final.log`. Query-count and equal-timestamp keyset controls are included. |
| NEW-1: `tests/unit/engine/orchestrator/test_call_mode_session.py::test_verify_persists_mismatch_and_refuses_success`; `tests/integration/pipeline/test_run_mode_end_to_end.py::test_cli_http_replay_has_no_network_and_verify_persists_mismatch[metadata]`, `-n 0` | Exception policy: **1 failed, 19 passed**, `L/policy-red.log`; final metadata-only CLI test against original production file: **1 failed**, `L/policy-json-red.log`, exit 1. | Policy/sink/e2e/docs selection: exit 0, **47 passed**, `L/policy-final-green.log`. Metadata-only drift preserves content, reports exit 2 and the policy, and keeps the live sink artifact unchanged. |
| NEW-2/K037: `tests/unit/plugins/infrastructure/test_templates.py`, `tests/unit/web/sessions/routes/composer/test_template_depth.py`, and `tests/unit/engine/test_expression_parser.py`; names include `test_deep_template_is_a_typed_error_before_name_discovery`, `test_deep_template_name_scan_returns_typed_failure`, `test_yaml_deep_template_does_not_return_server_error`, `test_deep_expression_rejected_before_recursive_validation` | Initial selected run: exit 1, **7 failed, 2 passed**, `L/template-logs/red.log`, including raw recursion and HTTP500. One exploratory 10000-unary MemoryError case was subsequently removed as broader budget scope; do not count all seven as final regressions. | Final three complete files: exit 0, **318 passed**, `L/template-logs/focused-final.log`; strengthened HTTP test requires 200 **and** `is_valid=false`. Shallow and wide-AST controls pass. |
| K063: `tests/unit/plugins/transforms/test_batch_group_key_types.py::test_container_key_fails_entire_batch_without_recording_value`; `tests/integration/pipeline/test_row_type_violation_routing.py::test_a_batch_plugin_row_fault_routes_the_whole_batch_with_a_value_free_reason`, `-n 0` | Unit: **16 failed, 65 passed**, `/tmp/sweep-batch-red2.log`; pipeline: **16 failed, 1 passed**, `/tmp/sweep-batch-integration-red2.log`, exit 1. | Affected plugins/routing: exit 0, **340 passed**, `/tmp/sweep-batch-final-green.log`. Independent **106 unit/hash-helper** and **16 pipeline** tests passed in `L/batch-review-unit.log` and `L/batch-review-integration.log`. |
| K051: `tests/unit/engine/test_explicit_audit_attempt.py::test_audit_attempt_requires_an_explicit_keyword`, plus affected engine/retry suites | Original defaults: **8 failed**, `L/lows-attempt-red.log`; scheduler protocol restored-default mutant: **1 failed, 8 passed**, `L/lows-protocol-mutant-red.log`, exit 1. | Exit0, **610 passed**, `L/lows-attempt-final.log`; includes actual gate lease-recovery attempts `[0,1]` and nonzero transform retry forwarding. |
| K123: `tests/unit/deployment/test_aws_ecs_terraform_package.py::test_gateway_sidecar_supplies_every_required_runtime_environment_name -n 0` | Real sidecar unknown-name mutations, including digit suffix: each exit 1/**1 failed**; `L/lows-gateway-mutant-{red,digit-red}.log`. Removing the new guard with the same mutant: exit 0/**1 passed**, `L/lows-gateway-mutant-without-guard.log`. Bare-prefix mutant originally passed; fixed capture produces exit 1/**1 failed**, `L/lows-gateway-bare-prefix-after.log`. | Gateway selection: exit 0, **16 passed**, `L/lows-gateway-final.log`, including native Terraform planning. After the one-character bare-prefix correction: exit 0, **15 passed**, `L/lows-gateway-bare-prefix-final.log`; native Terraform was not unnecessarily rerun for that isolated test correction. |
| Example: `bash examples/replay_verify/run.sh` | New assertions before F10 integration stop at `AssertionError: explain omitted verify verdicts`; `/tmp/sweep-example-red.log`, nonzero exit. | Coordinator-observed exit 0; `/tmp/sweep-example-green.log` shows live 3 rows, replay 0 requests, verify 3/3 matches, explain/export verdicts, missing-source exit 1 from run and validate, settings-drift reason, changed-page mismatch exit 2, and virtual sink evidence. |

Batch errors are value-free and route every buffered row through the named error
sink; valid scalars remain accepted. The independent review checked all seven
updated plugin source hashes against the canonical helper. On the batch lane,
before combining F10's export-version change, the separate command
`pytest tests/integration/core/dag tests/unit/architecture -n 2` finished with
exit 0: **1989 passed, 2 skipped, 1 xfailed** in 924.19 seconds,
`/tmp/sweep-batch-dag-gates.log`.

Independent reviews found no remaining blocking implementation defect before the
whole-tree gate. The small
integration review's K123 bare-prefix finding was reproduced and fixed before the
final candidate. F10, batch, CLI, template/expression, attempts and comparison
policy received separate review; the independent tests above identify their
actual scope.

## Whole-tree failures and repairs

The first complete gate at `79ed2c3e5` exposed three sweep regressions in test
inventories. The same three checks on source baseline `11e037eaf` passed:
`/tmp/sweep-gate-failures-base.log`, exit 0, **3 passed in 8.83s**. They were
therefore attributed to this sweep, not dismissed as unrelated failures.

- The delegation table omitted the new public
  `ExecutionRepository.iter_verification_decisions_for_run` method. Its signature
  case was added while retaining exact equality against public methods.
- The verdict visibility test constructs recorded evidence through repository
  writers; it does not execute a production pipeline. It was moved from
  `tests/integration/audit/test_verification_visibility.py` to
  `tests/unit/core/landscape/repository_integration/test_verification_visibility.py`,
  and the repository-test inventory was updated. The production-path inventory
  remains unchanged, and all 14 visibility cases remain executed.
- F10 moved two export connection acquisitions by six source lines and added
  three Landscape-only verification reader connection acquisitions. The exact
  connection-domain inventory was updated from the scanner's measured identities;
  no Session mutation authority or scanner rule was changed.

The first two repairs are lane commit `0807e4792`: the original checks reproduced
**2 failed, 2 passed** (exit 1, `/tmp/sweep-f10-gate-red.log`); the full delegated
signature table, taxonomy checks and relocated visibility tests then passed
**68 tests** (exit 0, `/tmp/sweep-f10-gate-green.log`). The authority repair is
lane commit `1436bfa09`; the failed check and bidirectional manifest controls
passed **2 tests**, with extra controls detecting missing, duplicated and
misclassified identities. These lane commits change test files only. Their
patches are consolidated in repair bundle `2f1507616`, described below.

The combined gate also exposed DAG projection pins affected by F10's required
export default change. The source baseline passes the linear production case
(exit 0, **1 passed**, `/tmp/sweep-dag-gate-base.log`); the candidate fails it
(exit 1, **1 failed**, `/tmp/sweep-dag-oracle-red.log`). The entire compared linear
projection differs only at the run's semantic settings hash. Lane commit
`d988fdcd6` regenerates fourteen settings digests, one checkpoint full-history
digest, and the dependent case registry hash. The canonical producer, given an
in-memory change of only the export version back to v2, reproduces every old
digest. Reversing the fifteen substitutions reproduces the whole original
manifest byte-for-byte; an extra-change negative control fails. No rows, routing,
accounting, scenario inventory or other oracle content changed. All 36 production
run cases plus checkpoint recovery, its existing corruption control and registry
parity passed: **39 passed**, exit 0, `/tmp/sweep-dag-oracle-green.log`.

The Composer cancellation test separately has a demonstrated pre-existing race:
it ignores the result of a two-second startup wait. A forced 2.25-second startup
delay makes both baseline and candidate fail before either invocation is
recorded. Lane commits `12aecba56` and `89e86b8bb` replace elapsed-time assumptions
with bounded entry, cancellation and worker-finish handshakes, preserving every
audit assertion. The delayed-start control and related discovery tests pass.
Independent review rejected an intermediate unbounded wait, then verified the
complete repair: a non-abandoning-worker mutation now fails an explicit assertion
and drains workers, exiting 1 in 31.85s, rather than hanging until external
timeout. The original gate's terminal traceback confirms the same failure:
`assert len(recorder.invocations) == 2` observed zero, with zero planner attempts
and empty phase counts. This repair changes only the test.

The five reviewed lane patches above were combined in `2f1507616` in the separate
repair worktree while the original PostgreSQL stage remained frozen. A direct
`git diff --exit-code 79ed2c3e5..2f1507616 -- src elspeth-lints gateway
pyproject.toml uv.lock scripts` exited 0 with no output. The exact 38 failed
tests were then run together against this repair bundle:

```text
collected 38 items
38 passed in 36.30s
pytest exit=0
```

Logs: `/tmp/sweep-combined-repair-green.log` and
`/tmp/sweep-combined-repair-green.xml`; selection:
`/tmp/sweep-combined-repair-selection.json`. Comparing JUnit identities proves
the original failed set equals the repaired executed set, with no failed,
errored or skipped cases. This targeted result does not change the original
gate's failed status.

## Canonical gates and lint corpus

Baseline static summary:
`/tmp/sweep-20260925-current-base-static/20260924T171503Z-sweep-base-20260925-3578817/summary.txt`.
Raw terminal evidence, with the workspace pathname omitted:

```text
head=11e037eaf015fc3b3bd07b54f4084216e9df59f4
stage=ruff exit=0 seconds=0 fatal=1
stage=mypy exit=0 seconds=2 fatal=1
stage=contracts exit=0 seconds=25 fatal=1
stage=lints exit=1 seconds=165 fatal=0 findings=2355
after=11e037eaf015fc3b3bd07b54f4084216e9df59f4 01ba4719c80b6fe9 frozen=yes
RESULT=PASS
```

The lint exit is the known fail-closed corpus, **not** a clean lint gate.

The preceding replay-completion lane also ran the canonical Python/PostgreSQL
stages on the identical integrated source baseline `11e037eaf`. The coordinator
read its terminal summary directly at
`.claude/lanes/replay-outcomes-20260925/final-gates/20260924T163404Z-replay-completion-20260925-3128045/summary.txt`:

```text
stage=ruff exit=0 seconds=0 fatal=1
stage=pytest exit=0 seconds=1308 fatal=1 57642 passed, 100 skipped, 2 xfailed, 98 warnings in 1300.62s (0:21:40)
stage=testcontainer exit=0 seconds=1011 fatal=1 569 passed, 1 skipped, 57885 deselected, 12 warnings in 994.30s (0:16:34)
after=11e037eaf015fc3b3bd07b54f4084216e9df59f4 01ba4719c80b6fe9 frozen=yes
RESULT=PASS
```

This is another lane's completed baseline run, not a sweep-candidate result.
Combined with the sweep's baseline static gate above, it provides all six stage
results on the same source baseline. Targeted baseline repetitions for candidate
failures are recorded separately.

First candidate command: `scripts/full-suite-gate.sh --execute --detach --stages
ruff,mypy,contracts,lints,pytest,testcontainer --workers 8 --log-dir
/tmp/sweep-20260925-final`.
First candidate gate directory:
`/tmp/sweep-20260925-final/20260924T171916Z-review-sweep-20260925-3629168`.
Its terminal `summary.txt` and `.done` were read before advancing the branch.
Raw terminal evidence, with the workspace pathname omitted:

```text
head=79ed2c3e577eaca7a21ccae553b91d00be1e3a21
stage=ruff exit=0 seconds=1 fatal=1
stage=mypy exit=0 seconds=19 fatal=1
stage=contracts exit=0 seconds=24 fatal=1
stage=lints exit=1 seconds=176 fatal=0 findings=2356
stage=pytest exit=1 seconds=2114 fatal=1 38 failed, 57802 passed, 100 skipped, 2 xfailed, 81 warnings in 2106.34s (0:35:06)
stage=testcontainer exit=0 seconds=945 fatal=1 569 passed, 1 skipped, 58083 deselected, 12 warnings in 929.33s (0:15:29)
after=79ed2c3e577eaca7a21ccae553b91d00be1e3a21 01ba4719c80b6fe9 frozen=yes
RESULT=FAIL
```

All 38 failed identities are accounted for by the repairs above. The test and corpus-fixture
repair bundle was fast-forwarded into `fix/review-sweep-20260925` after this gate
finished. Another session's PostgreSQL suite ran concurrently during this first
gate; live host process inspection recorded that contention. The next broad-test
slot was handed to the already-waiting template-review session before queuing
the repaired candidate's final run.

The first candidate lint stage completed with **exit 1, 2356 findings**. Its
normalized comparison against the 2355-finding baseline is recorded in
`/tmp/sweep-lints-diff.json`: **added 1, removed 0**. The comparison instrument
passed self-comparison, inserted-finding and removed-finding controls. The one
addition is R5 at `src/elspeth/tui/screens/explain_screen.py:404` for
`isinstance(self._state, LoadedState)`. This is owned-union narrowing explicitly
permitted by ADR032; it remains ready for narrow adjudication, with no suppression
or judge-signature edit. The legitimate v3 bound-test fingerprint was refreshed
from lint authority, and persisted SQL call_type requires exact str; neither
creates a remaining added finding. Batch/template lane comparisons had no added
findings.

Final repaired candidate: `2f1507616cc33bbb4dca586fc822ab959896876f`.
Final six-stage gate: **PASS, frozen=yes**, with no sibling pytest processes at launch.
Command: `scripts/full-suite-gate.sh --execute --detach --stages
ruff,mypy,contracts,lints,pytest,testcontainer --workers 12 --log-dir
/tmp/sweep-20260925-repaired-final`.
Summary: `/tmp/sweep-20260925-repaired-final/20260924T190906Z-review-sweep-20260925-589286/summary.txt`.
Its completed lint stage again returned exit 1 and 2356 findings. The controlled
full-corpus comparison in `/tmp/sweep-repaired-lints-diff.json` matches the first
candidate: one added owned-state R5, no removals.
Raw terminal summary, read with the `.done` marker present:

```text
head=2f1507616cc33bbb4dca586fc822ab959896876f
stage=ruff exit=0 seconds=0 fatal=1
stage=mypy exit=0 seconds=2 fatal=1
stage=contracts exit=0 seconds=22 fatal=1
stage=lints exit=1 seconds=147 fatal=0 findings=2356
stage=pytest exit=0 seconds=1326 fatal=1 57841 passed, 100 skipped, 2 xfailed, 98 warnings in 1318.86s (0:21:58)
stage=testcontainer exit=0 seconds=970 fatal=1 569 passed, 1 skipped, 58084 deselected, 12 warnings in 954.35s (0:15:54)
after=2f1507616cc33bbb4dca586fc822ab959896876f 01ba4719c80b6fe9 frozen=yes
RESULT=PASS
```

The default suite's JUnit document independently records 57,943 cases, zero
failures, zero errors and 102 skips (including the two expected failures).
PostgreSQL's separate completed selection covers the required F10 database gate.
The script treats the deliberate lint corpus as nonfatal; its PASS does not mean
the lint command passed or that operator HMAC signatures were verified.

## Delivery state

Branch: `fix/review-sweep-20260925`. Final verified code/test head:
`2f1507616cc33bbb4dca586fc822ab959896876f`. The final delivery commit adds only this
report after the frozen gate; resolve its identity with
`git log -1 --format=%H fix/review-sweep-20260925 -- docs/arch-analysis-2026-09-23-2016/temp/sweep-report.md`.
The delivery commit is reported in the handoff alongside this report's path.
All integrated implementation commits listed above and the repair bundle were
rechecked with `git merge-base --is-ancestor <commit> HEAD`: every exit was 0.
No sweep merge into release or push was performed. Later release fixes listed
above still require normal integration review when the maintainer lands this
branch; the gate certifies the recorded review candidate, not that future merge.

## Tracker availability

The sweep was atomically claimed as `elspeth-5ff7f2009a` by
`codex-review-sweep`. During final validation, the MCP tracker transport closed.
The CLI fallback, `filigree show elspeth-5ff7f2009a --json`, exited 1 with
`NOT_INITIALIZED`: no project Filigree configuration or data directory was
available. Release subsequently records the deliberate local tracker retirement
in `d479eb2b4`. The shared tracker was not recreated. Consequently, this report does
not claim a final tracker transition; branch commits and gate logs are the
delivery evidence.
