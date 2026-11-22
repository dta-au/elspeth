# CRAP hotspot risk assessment — 2026-11-22

Follow-up to the CRAP baseline ([`crap-baseline-2026-11-22.md`](crap-baseline-2026-11-22.md),
GitHub issue #378). The baseline ranks functions by a formula. This document reads the code
behind the ranking and says which hotspots need intervention now, which can wait for the next
touch, and which the score overstates.

**Scope.** The 19 functions in the issue's "Change hotspots" table, plus 22 others from the CSV
top 50 and the coverage-gap list that looked comparably dangerous. 41 functions in all. The
rest of the tree was not assessed.

**Evidence and labels.** Code was read in a `git archive` export of `main` @ `72fad2c23`; all
`file:line` references are to that commit and paths are under `src/elspeth/`. Unexercised lines
and branches come from the baseline's combined `coverage.json` (CI run 44477162225). In-flight
state was read with `git show` / `git log` on `candidate/0816-c5` (@ `3c56f9145`) and
`integration/0817` (@ `06c9cd408`). Every figure carries one of three labels:

- **measured**: produced by a command during this review (coverage.json, `git log`, an `ast`
  length script over `git show`);
- **read**: verified by reading the code or test at the cited lines;
- **inferred**: reasoned from what was read, not verified.

No test, suite or server was run. "Unexercised" means "not executed in the combined CI coverage
for the baseline commit". It does not mean no test exists: the PostgreSQL (`testcontainer`) job
and child-process code are outside that coverage.

## Executive summary

Seven functions are URGENT. They are ordered by likelihood × impact × exposure as judged from
the code, not by CRAP score.

| # | Function | Baseline | Size of first step | Single biggest reason |
|---|---|---|---|---|
| 1 | `_check_schema_contracts` | `web/composer/state.py:4818` | small, then medium | A hand-written mirror of the runtime graph builder's contract rules. It grew 2,563 → 3,362 → 3,540 lines in 90 days (measured), every commit since the baseline is a fix, and one recent agreement fix has never run in CI. |
| 2 | `run_tool_batch` | `web/composer/tool_batch.py:1050` | small, then medium | Every composer mutation and its audit row pass through one 2,700-line loop that closes each tool call by hand at about 17 exits. It is still growing on 0.8.16. |
| 3 | `AdvisorTranscriptHistory.from_records` | `web/composer/advisor_transcript.py:127` | small | Parses the whole session history on every composer turn, with about 20 fatal shape checks. The unexercised arms are the older-writer row shapes an upgraded store contains. One unreadable row stops every later turn in that session. |
| 4 | `BlobServiceImpl._reconcile_blob_deletions_locked` | `web/blobs/service.py:2575` | small | Crash recovery that restores or destroys blob bytes. Its whole loop body has never run under test. It runs first in every blob read, create and delete, and 0.8.16 puts prompt files behind it. |
| 5 | `ComposerServiceImpl._compose_loop` | `web/composer/service.py:2844` | small–medium | The block that decides which audit rows survive a crashed tool turn has 9 of its 12 plugin-crash raise sites unexercised. The loop is still growing. |
| 6 | `_plan_pipeline_inner` | `web/composer/pipeline_planner.py:3799` | small | Every planner provider call runs at least six whole-request passes on the event loop, over a message list that gains a whole pipeline per repair. This is the defect class 0.8.16 fixed next door, and the planner's copy is untouched. |
| 7 | `_bind_composer_value_transforms.field_type` | `web/composer/state.py:4660` | small | A third copy of the type-resolution walk. The half that handles multi-step graphs has never run in CI. Unchanged on both in-flight branches, so tests written now apply to all three trees. |

Four things to take from the rest of the assessment:

- **The highest CRAP scores are measurement artefacts.** `_Supervisor._run_owned` (3080),
  `supervise` (1260), `_read_snapshot` (756) and `_serve_request` (380) are child-process or
  PostgreSQL-only code with tests that the combined coverage cannot see. Fix the measurement,
  not the code.
- **Several large, high-scoring functions are not urgent.** `build_execution_graph` (cc 314),
  `_validate_with_probe_cache` (cc 322), `restore_from_journal` (cc 211) and
  `_replay_ssrf_request` (cc 78) are long but flat and fail closed.
- **In-flight pressure is confined to the composer.** Outside `web/composer` and
  `web/blobs/service.py`, no assessed function changed length between the baseline and either
  in-flight ref (measured).
- **No security-control bypass was found.**

For five of the seven URGENT items the first step is tests with no production change. A
differential harness for item 1 and item 7 already exists
(`tests/integration/pipeline/test_composer_runtime_agreement.py`,
`tests/integration/web/composer/parity/`), so the work is new cases, not new infrastructure.

## All assessed functions

"In flight" gives the function's length on the baseline → `candidate/0816-c5` (identical on
`integration/0817` in every case; measured).

| Function | Baseline | CRAP | Class | In flight | One-line reason |
|---|---|---:|---|---|---|
| `_check_schema_contracts` | `web/composer/state.py:4818` | 214.7 | **URGENT** | 3,362 → 3,540 | Runtime-rule mirror, fix-only churn, untested agreement fix |
| `run_tool_batch` | `web/composer/tool_batch.py:1050` | 265.8 | **URGENT** | 2,738 → 2,771 | Hand-copied call-closing tail at ~17 exits; state-preserving error arm untested |
| `AdvisorTranscriptHistory.from_records` | `web/composer/advisor_transcript.py:127` | 282.8 | **URGENT** | 273 → 295 | Whole-history parse per turn; fatal on older-writer rows it was never tested against |
| `_reconcile_blob_deletions_locked` | `web/blobs/service.py:2575` | 222.0 | **URGENT** | 59 → 59 | Destructive recovery path never executed under test |
| `_compose_loop` | `web/composer/service.py:2844` | 114.6 | **URGENT** | 848 → 901 | Audit-row failure primacy 9/12 unexercised; elision by list index |
| `_plan_pipeline_inner` | `web/composer/pipeline_planner.py:3799` | 210.0 | **URGENT** | 1,793 → 1,820 | Per-call whole-request work on the event loop; hatch outcome arms unexercised |
| `_bind_composer_value_transforms.field_type` | `web/composer/state.py:4660` | 423.2 | **URGENT** | 92 → 92 | Third copy of the type walk; forwarding half never ran |
| `ExecutionServiceImpl._run_pipeline` | `web/execution/service.py:3641` | 270.6 | SCHEDULE (first) | 1,563 → 1,563 | Silent `return None` arms for cancel races and recovery refusal are unmeasured; frozen in flight |
| `build_set_pipeline_candidate` | `web/composer/tools/sessions.py:656` | 146.7 | SCHEDULE | 1,465 → 1,652 | Flat, phased validator at 95.6%, but the fastest-growing function assessed |
| `_classify_and_budget_turn` | `web/composer/service.py:2142` | 104.7 | SCHEDULE (one item now) | 701 → 701 | A landed fix is no longer reached by its own regression test; two terminal tails have drifted |
| `_run_composer_turn` | `web/sessions/composer_turn.py:842` | 194.7 | SCHEDULE | 1,184 → 1,186 | 14 hand-copied audit-persist tails; the provider-rejection arm never ran with calls to persist |
| `ExecutionServiceImpl.recover_run` | `web/execution/service.py:2975` | 226.3 | SCHEDULE | 127 → 127 | 33% is mostly the PostgreSQL blind spot; five refusal arms not verified anywhere |
| `ExecutionServiceImpl.shutdown` | `web/execution/service.py:1998` | 126.4 | SCHEDULE | 108 → 108 | Every integrity arm leaves the join loop with no exit; none measured |
| `restore_from_journal` | `engine/barrier_coordination.py:1679` | 280.3 | SCHEDULE | 947 → 947 | Misses are fail-closed, but the "no mutation yet" section contains durable writes |
| `cli.py::resume` | `cli.py:3426` | 226.0 | SCHEDULE | 483 → 483 | Flat glue; the `headers: original` and dry-run arms are unmeasured |
| `cli.py::join` | `cli.py:4402` | 189.2 | SCHEDULE (low) | 535 → 535 | Flat glue; follower aggregation wiring never ran |
| `persist_turn_audit` | `web/composer/turn_audit.py:212` | 136.5 | SCHEDULE | unchanged | Rollback and unwind dispositions never ran; all fail closed |
| `_create_pending` | `web/coordination/repository.py:1176` | 149.7 | SCHEDULE | 386 → 386 | Misses are refusals; unbounded read of superseded review history |
| `WebScrapeTransform.process` | `plugins/transforms/web_scrape.py:1204` | 228.4 | SCHEDULE | 582 → 582 | Pagination time limits and a ten-way fetch dispatch partly unexercised |
| `_consume_policy_response` | `plugins/infrastructure/clients/http.py:1415` | 123.3 | SCHEDULE | 124 → 124 | Response-size and decompression limit branches never ran |
| `_count_json_source_rows` | `web/execution/fanout_guard.py:603` | 132.0 | SCHEDULE | 19 → 19 | 0%; loads the whole file; two exception classes escape the caller |
| `run_execution_lease_sql_finish_once` | `web/async_workers.py:848` | 169.5 | SCHEDULE (low) | 165 → 165 | Only the success path is measured |
| `_validate_with_probe_cache` | `web/composer/state.py:8482` | 322.3 | MONITOR | 1,673 → 1,677 | Long but flat at 98.5%; the 6 missing lines are refusals |
| `build_execution_graph` | `core/dag/builder.py:319` | 316.0 | MONITOR | 1,955 → 1,955 | Linear, phase-marked, fail-closed, 97.3% |
| `_replay_ssrf_request` | `plugins/infrastructure/clients/http.py:786` | 223.4 | MONITOR | 199 → 199 | Validates archived evidence, makes no network call; all 26 missing lines are raises |
| `_serve_request` | `plugins/infrastructure/templates.py:710` | 380.0 | MONITOR (fix measurement) | 83 → 83 | Child process; short, flat, typed replies |
| `_Supervisor._run_owned` | `composer_mcp/portable_supervisor.py:129` | 3080.0 | MONITOR (fix measurement) | 159 → 159 | Child process; 30 subprocess-driven tests; 2 commits in 90 days |
| `supervise` | `web/process_watchdog_helper.py:32` | 1260.0 | MONITOR (fix measurement) | 112 → 112 | Child process |
| `_read_snapshot`, `SessionComposerProgressAuthority.replay` | `web/coordination/composer_progress_authority.py:83`, `:394` | 756.0, 210.0 | MONITOR (fix measurement) | unchanged | PostgreSQL-only; 15 testcontainer tests; progress display only |
| `RepositoryRunProgressReader.read_after` | `web/execution/run_progress_reader.py:25` | 342.0 | MONITOR | 61 → 61 | PostgreSQL blind spot; flat, fail-closed |
| `_lock_cross_table_witnesses` | `core/landscape/data_flow/outcomes.py:167` | 126.6 | MONITOR | unchanged | Returns at once unless PostgreSQL; testcontainer tests exist |
| `collect_source_payload_refs` | `core/replay_payload_store.py:112` | 129.3 | MONITOR | 132 → 132 | 21 of 23 missing lines are integrity raises |
| `build_abandonment_review` | `web/composer/advisor_abandonment.py:105` | 230.7 | MONITOR | unchanged | The 22% is a dead mode: the only caller skips the uncovered loop |
| `_splice_topology_error` | `web/composer/tools/transforms.py:1180` | 126.7 | MONITOR | not checked | Flat refusal ladder; every miss is a fail-closed return |
| `_tutorial_launch_blocker` | `web/composer/tutorial_service.py:155` | 154.7 | MONITOR | 98 → 98 | Flat validator; only the pass path is unexercised |
| `_restore_render_receipt` | `plugins/transforms/pdf_rasterize.py:808` | 211.3 | MONITOR | 134 → 134 | All 22 missing lines are raises |
| `BlobDeletionPlan.__post_init__`, `BlobReplacementPlan.__post_init__` | `contracts/blobs.py:783`, `:858` | 113.1, 137.0 | MONITOR | unchanged | Constructor validation; misses are raises |
| `_Policy.expression` | `mcp/sql_policy.py:396` | 119.5 | MONITOR | 172 → 172 | Deny-by-default allowlist; misses are refusals |
| `_find_audit_databases` | `mcp/server.py:823` | 306.0 | MONITOR | 59 → 59 | CLI convenience; no write, no audit effect |

CRAP, complexity and coverage figures are the baseline's (measured there). Lengths are measured
here.

---

## URGENT 1 — `_check_schema_contracts` (`web/composer/state.py:4818`)

**Changed in flight (measured).** 3,362 lines and 58 closures at the baseline; 3,540 lines and
65 closures on both in-flight refs. `state.py` has 11 commits since the baseline, all
`fix(composer)` or `fix(templates)`; 16 of the 35 diff hunks land inside this function. Growth
over the 90 days: 2,563 (2026-08-24) → 2,779 (2026-09-24) → 3,362 → 3,540.

**What it is (read).** One function body whose closures share about a dozen captured locals
(`resolver`, `node_by_id`, `probe_cache`, `errors`, `parse_failed_producers`, three memo
dicts). It restates, for the composer, the contract rules the runtime enforces in
`core/dag/builder.py` and `core/dag/guarantees.py`; nearly every arm carries an
`Engine parity (elspeth-…)` comment. It runs on every composer validation.

**Failure modes.**

- *False green or false red.* A false green passes a pipeline that dies at run time; a false
  red refuses a valid request. The function's own comments record both having happened:
  `:5085–5097` ("made every union-coalesce pipeline validate green while the runtime build
  rejected it"), `:7338–7340` ("composed clean, passed /validate, and died on row 1"),
  `:7374–7377` (a false miss for pass-through columns), `:7470–7472` (a shared row_union
  divergence) (read). The 11 post-baseline commits are the same class (read from subjects).
- *Three hand-maintained copies of one walk.* `field_type` (`:4660`),
  `_resolved_producer_field_type` (`:6802`) and the runtime
  `core/dag/guarantees.py:585 resolve_guaranteed_field_type` implement the same rule. The two
  composer copies already differ (`:4717–4722` against `:6908–6916`) (read). A fix to one does
  not reach the others.
- *No memo on the composer type walk.* The runtime twin takes a `cache` its docstring calls
  load-bearing. `_resolved_producer_field_type` and `_connection_definite_emits` (`:6361`) have
  none, and the observed-source arm constructs and closes a source plugin on every call
  (`:6858–6871`, also `:4678–4692`) (read). The cost on a 25-node graph is **inferred**, not
  measured. The sibling vote and emit-profile walks are memoised (`:5445`, `:5916`, `:6175`),
  which is why this looks like an omission.
- *Abstention is silent success.* Each `return None` in the producer walk (`:5083`, `:5107`,
  `:5140`, `:5177`, `:5187`, `:5198`, `:5202`) skips the contract check for that edge. Most add
  a warning; `:5083` and `:5202` add nothing (read).
- *Order-dependent shared state.* `parse_failed_producers` is written at `:7243` and `:7527`
  and read at `:7237`, `:7518`, `:7643`, `:7728`, `:8077`. Whether a later rule runs for a node
  depends on which earlier loop saw its producer first (read).

**Unexercised decision points that matter (measured, checked against the code).**

| Lines | What it decides |
|---|---|
| `:7225–7235` | Rule A re-resolution at a row_union boundary when the presence walk abstained. This is the fix for elspeth-9d13900064; for a transform consumer it has never run in CI. |
| `:5125–5132`, `:5161–5166` | A parse error on a later fan-in arm becomes an abstention. Added to stop `/validate` crashing; never executed. |
| `:5167–5177` | row_union with a non-participating branch is skipped with a warning. |
| `:5190–5198` | The only loop guard on the walk-back. |
| `:6908–6916` | `union_merge` with a config-declared field, `any` against concrete: decides a certain-conflict refusal at a coalesce. |
| `:7500`, `:7472–7477`, `:8107–8109` | A sink with no direct producer falls back to walk-back. |
| `:7237`, `:7518`, `:7643`, `:7728`, `:8077` | The `parse_failed_producers` skips. |

Closure coverage (measured): `_walk_producer_entry_to_real_producer` 80%,
`_resolved_producer_field_type` 86%, `_value_producer_through_gates` 77%,
`_row_union_definite_emits` 67%.

**Why now.** It is the only large validator still growing, every change is a parity fix made
inside a closure nest where the fix cannot be tested alone, and the 0.8.17 file-held-prompt and
deferred-contract work lands in this function (the +121 and +96 line hunks; measured).

**First intervention.**

1. *Small.* Agreement cases for the table rows: each builds one small graph and asserts that
   the composer verdict equals the runtime build verdict. Start with `:7225–7235`,
   `:5167–5177`, `:6908–6916`, `:5125–5132`.
2. *Medium.* Memoise `_resolved_producer_field_type` on `(producer_id, field_name, mode)` and
   the source probe on source name, mirroring the runtime cache. Add a growth test at N and 2N
   nodes using `tests/unit/web/composer/_large_graph.py`.
3. *Large, on next touch, not as a project.* Lift the walk families out of the closure nest
   into a module that takes an explicit context object. Seams in order of independence: the
   header-spelling rules (`:7804–8140`), the type resolver with `_edge_field_type_conflict`
   (`:6789–7096`), the vote and emit-profile walks (`:5892–6503`). Mutation testing of the
   extracted type resolver then becomes practical.

**Composer invariants.** This is server-side validation of what the planner produced, which the
invariants permit. A split must not change which entries are errors and which are warnings, nor
the `error_code` strings and `SchemaContractDetail` payloads the planner's repair loop reads.
It adds no path that authors structure and no tutorial branch.

## URGENT 2 — `run_tool_batch` (`web/composer/tool_batch.py:1050`)

**Changed in flight (measured).** 2,738 → 2,771 lines; 289 → 301 decision nodes; 12 commits to
the file since the baseline. Five of the nine non-merge commits are approval-path or
result-shape fixes (read from subjects). The docstring (`:1057–1063`) says the body is a
verbatim move of the former `_dispatch_tool_batch` method with an alias preamble
(`:1065–1096`): it was relocated, not decomposed (read).

**What it is (read).** One `for tool_call in assistant_tool_calls` loop (`:1235–3762`) with 15
phases, each ending in its own `continue` or `break`, over about 20 loop-carried locals
(`state`, `last_validation`, `tool_outcomes`, `plugin_crash`, `advisor_failure`,
`proposals_this_turn`, …).

**Failure modes.**

- *The "close this call" tail is hand-copied at about 17 exits.* Each exit must record the
  audit row, append the tool outcome, update the anti-anchor tracker, append the `role: tool`
  message and set the budget flags. Sites include `:1327–1350`, `:1406–1433`, `:1706–1721`,
  `:2488–2513`, `:2954–2983`, `:3479–3496`, `:3729–3756` (read). They already differ: the
  blocked branch sets `turn_has_mutation = True` unconditionally at `:1719`, including for a
  read-only block at `:1672–1684`. A tool call with no matching `role: tool` message is
  rejected by the provider on the next request; a call with no recorder row breaks the
  one-invocation-per-dispatch audit contract. Nothing structural stops the next added exit
  from omitting a step.
- *`BaseException` is stored and re-raised later, with awaits in between.* `:1954–1958`,
  `:2157–2162`, `:2262–2264`, `:2345–2347` capture any `BaseException`, including cancellation;
  it is re-raised only inside `_do_dispatch` (`:3264–3265`). The function awaits again at
  `:2519` in between. This holds today only because no branch between capture and `:3264`
  matches the tool names concerned (read; not executed).
- *Cost per `set_pipeline` is 2–4 full candidate builds.* `:809–880` builds once and up to
  twice more when required controls or review requirements change the arguments; the approval
  path builds again at `:2140`; the non-approval path builds again inside `execute_tool`
  (`tools/sessions.py:2131`) (read; the count is not measured). Linear in graph size, but the
  multiplier is spread across three functions.
- *Full validation on the event loop.* `validate_composition_state(state)` is called at six
  sites when `last_validation is None` (`:1704`, `:1883`, `:2004`, `:2258`, `:2340`, `:2466`),
  none through the worker pool (read).

**Unexercised decision points that matter (measured, checked against the code).**

| Lines | What it decides |
|---|---|
| `:3543–3552` (`:3546` missing) | A preflight error after a successful mutation is rebuilt to keep the mutated `partial_state`. The state-preserving arm has never run. |
| `:1243–1244` | Cancellation seen before any tool started. The "no fabricated empty assistant turn" guarantee is untested; `_ToolBatchCancellationRequested` is named in no test (read on the current checkout). |
| `:2764–2796` | Advisor timeout while compose budget remains. The whole arm is unexercised; possibly unreachable (inferred). |
| `:2798–2799` | A first-party failure before the advisor call is reported as a crash rather than escaping unaudited. |
| `:1946 → 1960` | "No change" against "apply" after finalisation alters the candidate. |

**Why now.** Every composer edit on a large graph goes through this loop, the
in-flight commits are still adding branches to the approval path where the two duplicated
`set_pipeline` blocks live (`:1880–1958`, `:1996–2222`), and a long run of small edits exercises its
per-call cost.

**First intervention.**

1. *Small, no source change.* Three characterisation tests: (a) a preflight error after a
   successful mutation keeps the mutated state; (b) cancel before the first tool publishes no
   assistant row; (c) over a batch containing every refusal kind, each tool call id has exactly
   one recorder row and exactly one `role: tool` message, in call order. Test (c) is the one
   that catches drift between the exits.
2. *Small.* Count `build_set_pipeline_candidate` and `validate` calls per `set_pipeline` at two
   graph sizes, so "2–4" becomes a pinned number.
3. *Medium.* Extract the exit tail into one helper used by every site, then lift the advisor
   block (`:2535–2983`) into its own coroutine. It is the cleanest seam: it reads `state` but
   never rebinds it.
4. *Large, later.* Merge the two `set_pipeline` finalisation blocks into one function with a
   discriminated result.

**Composer invariants.** Each step moves code that validates, refuses, redacts or records what
the provider authored. The required-control splice (`:1907–1933`, `:2028–2054`, `:2308–2329`)
is the nearest thing to server-side structure here and must keep its property that it runs only
on a provider-authored, already-acceptable candidate. The function has no tutorial reference
(read) and must not gain one.

## URGENT 3 — `AdvisorTranscriptHistory.from_records` (`web/composer/advisor_transcript.py:127`)

Not in the issue's hotspot table; included from the top 50 (CRAP 282.8, cc 138, 80.3%).

**Changed in flight (measured).** 273 → 295 lines (now at line 200); 7 commits to the file,
which grew 901 → 1,345 lines. The 0.8.16 commits bound what the advisor is *sent*. They do not
bound what this function *parses*: the call site is the same on `candidate/0816-c5`
(`web/sessions/composer_turn.py:1505–1506`; read).

**Failure modes.**

- *One unreadable historical row stops every later turn in the session.* The function is
  evaluated inline as an argument to `composer.compose(...)` at
  `web/sessions/composer_turn.py:1505–1511`, on every ordinary turn, whether or not the advisor
  is used (read). It raises `AuditIntegrityError` on about 20 row-shape conditions (`:143`,
  `:147`, `:152`, `:172`, `:178`, `:201`, `:245`, `:268`, `:293`, `:302`, `:309`, `:334`,
  `:356`, `:365`, `:379`, `:395`, `:398`). The enclosing `try` (`:1418`) has no arm that
  handles `AuditIntegrityError` before the provider call (read), so the turn fails. Because the
  input is the stored history and the function is deterministic, the next turn fails the same
  way (inferred). The user-visible outcome was not traced.
- *The unexercised arms are the older-writer shapes.* See the table below. 0.8.16 will read
  stores written by 0.8.15, so rows this reader was never tested against
  are what it meets first.
- *Work grows with session history, on the turn path.* Per turn, over the whole history:
  SHA-256 of every stored `arguments_canonical` and `result_canonical` (`:308`), which for a
  `set_pipeline` row is the full graph; a linear scan of `events` for each tool row's parent
  (`:345–347`), quadratic in row count; `json.loads` of every visible payload (`:381`); and a
  final `json.dumps` of all events (`:399`) (read). At the baseline this runs on the event
  loop. This is the project's recurring defect class in the subsystem that has already ended
  long sessions. Not timed.

**Unexercised decision points that matter (measured, checked against the code).**

| Lines | Row shape handled |
|---|---|
| `:165 → 170–176`, `:263–266` | A response-coverage row whose public call was already checkpointed by the provider owner |
| `:272 → 275` | Stored content that does not start with the raw provider prose |
| `:306 → 307` | A legacy invocation with no result canonical and no hash |
| `:336 → 337` | A visible tool event on a row not written by the compose loop |
| `:360 → 365` | Visible tool evidence with no owned parent (raises) |
| `:388 → 391` | A visible event with no retained selected result |
| `:394 → 395` | A tool row with `tool_calls` but no invocation envelopes (raises) |
| `:146 → 147` | Out-of-order `sequence_no` (raises) |

**Why now.** Long-lived, large-graph sessions are about to be read by a new version. The first evidence of a gap would be a session that cannot take another turn.

**First intervention.**

1. *Small.* Characterisation tests with row fixtures for each shape in the table, asserting
   the produced event or the specific refusal. Include rows as the 0.8.15 code wrote them.
2. *Small.* A growth-ratio test over histories of N and 4N turns, each with a large
   `set_pipeline` row, asserting time and `events_json` size ratios.
3. *Medium, after a ruling.* Split the per-row projection (`:142–396`) into a pure
   `_project_row(row, ctx)`. The ruling needed: should an unreadable historical row fail the
   turn (current behaviour), or mark advisor history unavailable and let the turn proceed? The
   second trades an integrity check for availability, so it is a decision, not a refactor.
   Parsing only the window that will be sent raises the same question.

**Composer invariants.** This assembles evidence for the advisor request; it authors no
structure. A degraded-history mode must still send the request to the provider with an honest
"history unavailable" marker and must never substitute a server-written summary.

## URGENT 4 — `BlobServiceImpl._reconcile_blob_deletions_locked` (`web/blobs/service.py:2575`)

**Changed in flight (measured).** The function is 59 lines on all three refs (at `:2585` in
flight). The file has 4 commits since the baseline, all file-held-prompt work (read from
subjects).

**What it does (read).** For every row in the session's blob-deletion ledger, other than the
blob the caller is itself deleting, it finishes or undoes an interrupted deletion. Phases
`intent` and `staged` (`:2600–2620`) restore the tombstone over the canonical path
(`os.replace` at `:2607`) and abort the ledger row unless the caller is a read (`:2616`). Phase
`purge_pending` (`:2621–2632`) moves the canonical file to the tombstone (`:2629`) and purges
it. Six inconsistencies raise `AuditIntegrityError` (`:2603`, `:2612`, `:2614`, `:2619`,
`:2626`, `:2633`). It is called first, under the custody lock, by create (`:2743`), delete
(`:3351`) and every content read (`:3378`).

**Coverage (measured).** 14.3%: 35 of 42 statements and 25 of 28 branches missing. Every line
from `:2598` to `:2633` is unexercised. The only branch taken at `:2596` is the skip.

**Why the existing tests miss it (read at the baseline commit).**
`tests/unit/web/blobs/test_service_recovery_ack.py` injects an acknowledgement loss at each
phase and then retries `delete_blob` for the *same* blob. `delete_blob` passes
`exclude_blob_id=blob_id` (`:3351`), so the retry skips this loop.
`tests/unit/web/blobs/test_mutation_admission.py:122` replaces the method with a failing stub.
A search of `tests/` at the baseline and on `candidate/0816-c5` for the method name finds only
that stub (measured; the search was controlled against a known-present name). No test under
`tests/testcontainer/web` is named for cross-operation deletion recovery; their bodies were not
read.

**Failure modes.**

- *Session-wide blob outage after one interrupted delete.* Any of the six raises fires before
  the caller's own work on every later read, create and delete in that session, because a
  failed recovery does not clear the ledger row (read). Whether each raise is reachable from a
  legitimate crash point is exactly what is untested (inferred likelihood).
- *Wrong direction of recovery.* Restore-or-destroy is chosen from `plan.phase` and two
  `exists()` checks. `_require_exact_deletion_bytes` guards both renames (`:2605`, `:2627`), so
  the residual risk is destroying the right bytes of a deletion that should have been undone
  (inferred). No test pins which crash points restore and which purge.
- *The read path mutates and leaves debt.* A read restores the tombstone (`:2607`) but does not
  abort the row (`:2616`) and skips `purge_pending` (`:2622–2623`). The asymmetry is deliberate
  and has nothing to fail against (read).

**Why now.** 0.8.16 moves long prompts into files held as blobs, and the advisor and preflight
read them. In a graph with about 12 LLM nodes, all of that prompt text sits behind this
function on every read. A recovery path that has never executed under test is now in front of
users' prompts.

**First intervention (small, no production change).** In `test_service_recovery_ack.py`,
reusing its `lose_phase_ack` injection: strand blob A at `intent`, `staged` and
`purge_pending`, then call a content read on blob B, a create, and a delete of blob B. Assert
the final ledger rows, which of A's files exist, and that A's bytes are fully restored or fully
gone. Add the two-file and no-file states and assert the refusal. That is a 3 × 3 matrix plus
about four refusal cases. Check `_reconcile_blob_replacements_locked` (called at `:2583`) for
the same gap; it was not assessed. No seam needs splitting. Storage code; the composer
invariants are not engaged.

## URGENT 5 — `ComposerServiceImpl._compose_loop` (`web/composer/service.py:2844`)

**Changed in flight (measured).** 848 → 901 lines; 12 commits to the file, ten of them
`fix(composer)`. The 0.8.16 change adds an except arm that returns an earlier admitted reply,
a fourth return site (read from the diff). `service.py` is the most-changed file in the tree
(153 commits in 90 days; baseline, measured).

**What it is (read).** Phases P1–P5 are already separate methods. What remains in the loop is
what was not extracted: about 15 loop-carried locals (`:2995–3072`), a closure that snapshots
14 of them through default arguments (`:3209–3224`), and a 200-line block that decides which
exception wins after a tool turn (`:3429–3624`).

**Failure modes.**

- *Failure primacy decides what the audit trail keeps, and most of it never runs.* After
  dispatch and persist the loop picks, in order: plugin crash, advisor failure, deferred
  cancellation, early-checkpoint failure, advisor timeout, early deadline, effect-verification
  failure. The plugin-crash branch has 12 raise sites over three persistence states (turn
  persisted, P4 rolled back, no audit target). The comment at `:3459–3463` states the stake:
  `recorder.invocations` is request-cumulative, so carrying the wrong slice "would duplicate
  already committed rows when the route drains the crash". The slice arithmetic is at
  `:3464–3471` (read). If wrong: duplicated or missing tool-invocation audit rows on a crashed
  turn.
- *Context elision by stored list index.* A superseded reply is appended at an index captured
  at `:3115` and removed by `del llm_messages[index]` at `:3152`; advisor injections are
  remembered as indices (`:3194–3204`) and deleted later (`:3426–3427`). The indices stay valid
  only while nothing else inserts or removes earlier in the list. Today nothing does (read).
  Deleting the wrong index leaves an unpaired protocol message, which providers reject. Any
  change that sheds context in place breaks this silently.
- *Loop state captured by default arguments.* A loop variable added later and used in the
  closure without being added to the default-argument list reads the live value instead of the
  snapshot (read). Nothing enforces the list.
- *Growth.* Discovery turns have no aggregate cap, so `llm_messages` grows until the deadline,
  and `transcript.observe_messages(llm_messages)` runs over the whole list every tool turn
  (`:3328`) (read; not timed).

**Unexercised decision points that matter (measured, checked against the code).** In the
plugin-crash branch, the raise sites that did not run are `:3448`, `:3451`, `:3477`, `:3480`,
`:3482`, `:3491`, `:3494`, `:3496` and `:3500`: every arm where the crash has no recorded
cause, the P4-rolled-back arm with pending checkpoint failures, and the whole no-audit-target
branch (`:3489–3500`). Nine of the twelve sites; the three that ran are `:3453`, `:3457` and
`:3486`. The slice guard at `:3465 → 3466` did not fire. Also unexercised: `:3509 → 3510` (an
advisor failure whose audit row could not be persisted) and `:3529 → 3530`. No test file names
`plugin_crash_cause` (read on the current checkout, which is an older branch).

**Why now.** The loop is still gaining exits, and the unexercised block is the one that
protects composer audit rows when a turn fails, which is the case hardest to notice in normal
use.

**First intervention.**

1. *Small–medium.* Extract `:3429–3624` as a function that reads loop state and only raises.
   Cover it with a table test over the three persistence states × cause present × pending
   checkpoint failures, asserting the exception type, its `tool_invocations` slice and its
   `__cause__`. This is the best risk reduction per line in the composer loop.
2. *Small.* Replace index bookkeeping with identity (keep the message objects to elide, remove
   by `is`), with a test of two stacked injections.
3. *Medium.* Move the loop-carried locals into one loop-state object passed to the closure.

**Composer invariants.** Each step moves code that runs after the provider has answered; none
adds a way to finish a turn without a provider call. The 0.8.16 fallback returns a reply the
model already wrote and a refactor must keep it so. No line of `:2142–3692` contains
"tutorial" (measured).

## URGENT 6 — `_plan_pipeline_inner` (`web/composer/pipeline_planner.py:3799`)

**Changed in flight (measured).** 1,793 → 1,820 lines; 12 commits to the file. 58 of the
file's 89 commits in 90 days have "fix" in the subject. None of the in-flight hunks touches the
per-call preparation block or the escape-hatch arms described below.

**What it is (read).** About 215 lines of setup, 14 nested closures (`call_model_body` is about
580 lines), then one `while True` of about 880 lines. About 20 loop-carried locals are mutated
by closures through `nonlocal`. The loop has four phases that are natural seams: provider call
and failure triage (`:4718–4809`), protocol guards (`:4810–4911`), terminal candidate
evaluation (`:4913–5292`), discovery batch (`:5294–5591`).

**Failure modes.**

- *Whole-request work on the event loop, per provider call, growing with each repair.*
  `call_model_body` is `async` and does this inline rather than through `run_planner_sync`:
  fit the context budget over every message (`:4038`), apply cache markers (`:4052`),
  `canonical_json` then `json.loads` of every message and tool (`:4061–4068`), build the
  capability manifest (`:4082`), measure request size (`:4092`), then `observe_messages`,
  `capture_provider_context` and `prepare_delivery` (`:4224–4229`) (read). That is at least six
  passes per call. `messages` "keeps growing unshed" (comment, `:4036–4037`): each rejected
  candidate appends the model's whole tool call, which is the whole pipeline, plus feedback
  (`:5061–5069`, `:5128–5144`, `:5274–5282`). For a ~200 KB graph a request with a few repairs
  is several hundred KB, serialised several times while the loop is held (inferred from sizes;
  **not timed**). `run_planner_sync` exists and is used for setup (`:3844–3859`) but not here.
- *On an escape-hatch turn, every failure is replaced by the earlier exhaustion.* The
  `except Exception` arms at `:4804–4808` and `:5284–5288` and the arm at `:4996–5001` raise
  `hatch_error from None`. `AuditIntegrityError` is an `Exception`, so an integrity fault on
  the senior model's turn reaches the user as the exhaustion code; only the trail keeps the
  real cause (read). This may be intended. None of it is exercised, so the intent is not
  pinned.
- *Exhaustion handling written out seven times.* `:4762–4771`, `:4790–4796`, `:4891–4901`,
  `:4916–4924`, `:5026–5053`, `:5108–5126`, `:5239–5266`, with small differences (read).

**Unexercised decision points that matter (measured, checked against the code).**

- Hatch turn fails and the original exhaustion stands: `:4755 → 4758`, `:4783 → 4786`,
  `:4908 → 4909`, `:4998 → 4999`, `:5103 → 5104`, `:5285 → 5286`. Six arms.
- Exhaustion engages the hatch: `:5203 → 5204–5225` (blind repeat), `:5571 → 5572` (argument
  cycle).
- `:5419`, `:5426 → 5427`, `:5446 → 5447`: sibling-cancellation drain in the discovery batch.

Tests name each exhaustion code and the hatch (read on the current checkout); the combinations
of code × hatch availability × hatch turn are what is missing.

**Why now.** This is the provider chokepoint, the in-flight work is all context-size work that
changes what `messages` holds, and the per-call cost is the defect class 0.8.16 fixed in
`service.py` while leaving the planner's block alone.

**First intervention.**

1. *Small.* Move the per-call preparation (`:4038–4096`) into one synchronous function and
   call it through `run_planner_sync`. No decision changes. Add a growth-ratio test at two
   message sizes. Time it first, so the size of the problem is measured before it is fixed.
2. *Small.* A table-driven characterisation test over failure kind × hatch available × is hatch
   turn, asserting the raised code and the trail, with a scripted provider.
3. *Medium.* Collapse the seven exhaustion blocks into one helper, then split along the four
   phases, carrying the counters in one dataclass.

**Composer invariants.** Both model roles stay provider calls and no path returns a plan
without one; tests should assert a provider call per transition, not per walk. The one place
the server changes the planner's candidate is the `candidate_finalizer` seam (`:4983–5009`),
which auto-wires required controls. A decomposition must not move or widen it. Because step 1
is a latency change on the planner entry path, it falls under the standing review trigger in
`AGENTS.md`: it moves work off the event loop and must not remove or skip a provider call. No
line of `:3799–5592` contains "tutorial" (measured).

## URGENT 7 — `_bind_composer_value_transforms.field_type` (`web/composer/state.py:4660`)

Highest CRAP of the composer hotspots (423.2) and the cheapest to fix. Best done with item 1.

**Changed in flight (measured).** 92 lines on all three refs; no hunk touches it. `git log -L`
shows one commit ever (cf2d347da, 2026-09-29).

**What it is (read).** A closure of `_bind_composer_value_transforms` (`:4626`), called once
per validation at `:4891`. It resolves the type of each field a `value_transform` expression
reads; `bind()` (`:4753`) then writes those types into the shared probe instance for the node
(`:4802`), which later contract rules read.

**Failure modes (read).**

- A wrong type makes `bind_upstream_input_types` raise, which `:7098–7099` turns into a
  high-severity `value_transform_result_type_incompatible` error on a valid pipeline. Or a
  wrong type binds silently and skews the union-coalesce conflict check (`:7109–7149`).
- A wrong `None` (`:4722`, `:4744`) leaves the field unbound; the runtime builder
  (`core/dag/builder.py:1823`) binds it and may refuse a graph the composer passed.
- Divergence from `_resolved_producer_field_type` (`:6802`) is undetected: no test compares
  them.
- `except Exception` at `:4686`, `:4703` and `:4805` turns anything the probe-exception
  classifiers accept into "unresolved" with no entry.

**Unexercised (measured: 32 of 71 statements, 24 of 42 branches).** The whole forwarding half,
`:4714–4751`: carried renames, a config-declared field with no stamp, pass-through and
forwarded-field inheritance, the two "cannot prove" arms and the upstream recursion. In CI
every transform-producer call returned from a declaration at `:4713` or earlier. The ordinary
shape *source → pass-through transform → value_transform reading a source field* has therefore
never been exercised through this function. Also `:4686–4689` (source probe failure) and
`:4710` (an upstream value_transform with a bind error).

**Tests (read).** One dedicated test file, 64 lines, one test
(`tests/unit/web/composer/test_value_transform_bind_attribution.py`), present at the baseline.

**Why now.** It is unchanged on both in-flight branches, it is small, and the untested half is
the half that handles multi-step graphs.

**First intervention (small).** Five agreement cases in `test_composer_runtime_agreement.py`:
a `value_transform` reading (a) a source field through a pass-through transform, (b) a renamed
field, (c) a field behind a forwarding transform that removes it, (d) a field behind a second
`value_transform` with an unresolved target, (e) an observed source whose probe fails. Then
(medium) make `field_type` call the same resolver as `_resolved_producer_field_type`, leaving
one composer copy. Validation only; the composer invariants are not engaged.

---

## SCHEDULE (next touch)

**`ExecutionServiceImpl._run_pipeline`** (`web/execution/service.py:3641`) — first in this
list, and the closest call. It is the only path by which the web product runs a pipeline. The
arms that decide cancel races (`:3698–3700`, `:3743–3750`, `:3760–3765`), the silent downgrade
of `resume_existing` (`:3758–3759`), recovery refusal (`:4829–4831`), the live-leader check
(`:4835–4838`) and the governed happy path (`:4385 → 4405`) are unmeasured, and most end in a
silent `return None` (read, measured). The `finally` closes three resources with no
per-resource guard (`:5176–5181`), so one failing close skips the rest and replaces the run's
own exception (read). Two `next(...)` calls with no default (`:4048`, `:4419–4421`) surface a
missing blob id as a bare `StopIteration` (read). It is not URGENT because the function is
frozen (same length on all three refs, one in-flight commit that only moves it; measured) and
because real process-death tests exist under `tests/testcontainer` that the combined coverage
cannot see, so some of these arms may be covered there (**not verified**). It becomes urgent
the moment a change to it is planned. First step (small–medium): confirm what the testcontainer
job covers, then characterisation tests driven through the existing `ExecutionTestCustody`
fixtures, asserting terminal status, saga state and events. Then extract the admission and
permit block (`:3682–3775`) into a function returning one decision value.

**`build_set_pipeline_candidate`** (`web/composer/tools/sessions.py:656`). A staged validator
with a clear spine, linear in component count, that never mutates its input (read). The misses
are single rejection arms; add tests for them before the next touch. It is the fastest-growing function assessed (+185 lines, +43 decision nodes on
0.8.16; measured) and is the unit of cost in `run_tool_batch`. Next touch (medium): split along
its numbered comments into `_validate_sources`, `_validate_nodes`, `_validate_outputs`. A split
must not add defaults that create nodes, edges or outputs the provider did not send.

**`ComposerServiceImpl._classify_and_budget_turn`** (`web/composer/service.py:2142`). One small
item to do now: the staged-review repair gate at `:2362–2385`, added for elspeth-85f3cc3022,
was not reached in the baseline (branch `:2362 → 2380`; measured), and its named regression
test now passes through an earlier refusal route (read on the current checkout). The gate is
either dead code or a fallback with no test; decide which. Also: the two terminal tails
(`:2510–2652`, `:2710–2825`) have drifted, and the second omits `publication_binding` at
`:2773` (read; effect not traced). Both "advisor deadline expired after the model replied" arms
(`:2540–2555`, `:2753–2770`) are unexercised. Next touch (medium): one helper for both tails,
keeping the provider call in each.

**`_run_composer_turn`** (`web/sessions/composer_turn.py:842`). 14 hand-copied "persist the
audit, mark durable" tails across its except arms (measured by search). The `_BadRequestLLMError`
arm (`:1650–1664`), which a gateway context-length rejection takes and which 0.8.16 edits, has
never run with LLM calls attached, so the audit write for a provider-rejected call is
unexercised (measured). The same holds for `:1863–1877`. First step (small): one test per arm
with a call attached, asserting the audit row.

**`ExecutionServiceImpl.recover_run`** (`web/execution/service.py:2975`). The 33% is mostly the
PostgreSQL blind spot: three real process-death tests exist in
`tests/testcontainer/web/test_cross_process_execution_handoff_postgres.py` (read). Not verified
as covered anywhere: the cancel-requested arm (`:2988–2994`), the governance-on arm
(`:2998–3027`), the archived-session arms (`:3001–3009`, `:3044–3052`) and the submit-failure
cleanup (`:3088–3100`). The code is linear guards that each end in a recorded refusal. First
step (small): unit tests for the five refusal arms.

**`ExecutionServiceImpl.shutdown`** (`web/execution/service.py:1998`). Each integrity arm
records an error and continues without marking the task observed (`:2034–2040`, `:2051–2056`,
`:2061–2069`, `:2070–2076`), so the join loop polls every 10 ms with no exit and `:2102`, which
would raise the recorded failures, is never reached (read). A comment at `:2012–2014` says this
is deliberate; it relies on an outside supervisor (inferred). None of the arms is measured.
First step (small): tests that force each arm; decide whether a bounded wait is wanted.

**`BarrierRecoveryCoordinator.restore_from_journal`** (`engine/barrier_coordination.py:1679`).
Of 26 missing lines, 23 are integrity raises, 2 are log lines and 1 is a `continue` (measured,
read). No commits in flight. Two things to fix at next touch. The docstring (`:1688–1692`) and
the header at `:1835` say nothing mutates before all derivations succeed, but durable journal
releases happen inside that section at `:1985`, `:2146` and `:2382`, before derivations that
can still raise (read). A re-run should converge (inferred), but the next author will trust the
header. And a short reset count is a warning for coalesce (`:2509–2516`) but an error for
row_union (`:2541–2544`), with neither arm measured. Split (medium) along the plan lists the
function already builds.

**`cli.py::resume`** (`cli.py:3426`) and **`cli.py::join`** (`cli.py:4402`). Flat command glue;
most misses are a message and an exit. Gaps that carry behaviour: in `resume`, the
`headers: original` field-resolution arm (`:3705–3726`) and the dry-run arm, which is the
default mode (`:3649–3654`); in `join`, follower aggregation wiring (`:4702–4708`) and the
leaderless and SIGINT exits (`:4780–4827`). `join` is driven in-process by an existing test, so
its gap is real, not a subprocess artefact (read). Three `CliRunner` tests (small).

**`persist_turn_audit`** (`web/composer/turn_audit.py:212`). Stable; every unexercised arm
fails closed and no unredacted persist path is visible (read). The rollback and unwind
dispositions (`:556–568`) decide whether the driver keeps in-flight invocation evidence after a
failed audit write and have never run. Two tests (small).

**`_RepositoryInterpretationMutations._create_pending`** (`web/coordination/repository.py:1176`).
All 21 missing lines are refusals (measured). The concern is growth: `:1245–1255` reads every
superseded event for the node with no limit, and `:1196` and `:1207` each load a whole
composition state under lock (read). Calls per turn were not measured. First step (small): a
growth test with superseded history of 1 and of 50.

**`WebScrapeTransform.process`** (`plugins/transforms/web_scrape.py:1204`). The SSRF checks on
the first URL and each discovered URL are on exercised lines (read). Unexercised: all four
pagination time limits (`:1620`, `:1630`, `:1647`, `:1660`), the mid-pagination retry branch
(`:1646–1651`), and three arms of a ten-way `_fetch_url` dispatch (`:1442–1475`) in which a new
request option must be added to several arms (measured, read). First step (small): collapse the
dispatch into one keyword call.

**`AuditedHTTPClient._consume_policy_response`** (`plugins/infrastructure/clients/http.py:1415`).
Enforces response-size, decompression-ratio and deadline limits. First step (small): add
hostile-response tests.

**`_count_json_source_rows`** (`web/execution/fanout_guard.py:603`). 0% and no test names it
(measured; read on the current checkout). It loads the whole JSON source at admission
(`:614`). Five tests (small).

**`run_execution_lease_sql_finish_once`** (`web/async_workers.py:848`). Only the success path
is measured. Three waits (`:963`, `:979`, `:998`) poll with no deadline of their own (read).
Tests for admission timeout and one setup failure (small).

## MONITOR (the score overstates these)

- **`CompositionState._validate_with_probe_cache`** (`web/composer/state.py:8482`). About 25
  sequential passes; the complexity is per-node-type ladders. It mutates only local lists. The
  6 missing lines are refusals or no-ops (measured, read). +3 lines in flight. One hazard to
  confirm (inferred): the semantic stage (`:9775`) reads the shared probe instances before
  `_check_schema_contracts` (`:10142`) binds value-transform types into them (`:4802`).
- **`build_execution_graph`** (`core/dag/builder.py:319`). Twenty marked phases, each
  validating and raising; nothing persisted. 12 of 15 missing lines are raises. The one real
  untested shape is `:1464`. Mutation testing of the edge-matching block (`:1429–1481`) would
  be the best single check.
- **`AuditedHTTPClient._replay_ssrf_request`** (`clients/http.py:786`). Makes no network call;
  validates archived call evidence field by field. All 26 missing lines are integrity raises;
  origin checks on hops and the final URL are on exercised lines (measured, read).
- **Child-process code:** `_serve_request` (`templates.py:710`), `_Supervisor._run_owned`
  (`composer_mcp/portable_supervisor.py:129`), `supervise` (`web/process_watchdog_helper.py:32`).
  All three read 0% because they run only in a child process. `_run_owned` has 30
  subprocess-driven tests (measured count), 2 commits in 90 days, and serves the stdio MCP
  composer, not the web composer (inferred from the package).
- **PostgreSQL-only code:** `_read_snapshot` and `replay`
  (`web/coordination/composer_progress_authority.py`), `read_after`
  (`web/execution/run_progress_reader.py:25`), `_lock_cross_table_witnesses`
  (`core/landscape/data_flow/outcomes.py:167`). Testcontainer tests exist for each (read by
  name; bodies not read for the last).
- **`build_abandonment_review`** (`web/composer/advisor_abandonment.py:105`). The uncovered
  loop runs only when `visible_transcript is None`; the only production caller
  (`web/composer/turn_effects.py:618–626`) always passes it (read). Delete the dead mode at
  next touch.
- **Flat validators whose misses are all refusals:** `_splice_topology_error`,
  `_tutorial_launch_blocker`, `_restore_render_receipt`, the two blob plan `__post_init__`
  methods, `_Policy.expression`, `collect_source_payload_refs`. `_tutorial_launch_blocker` is
  a launch-time rejection check, which the composer invariants allow; unit tests replace it
  with a stub, so only the live tutorial run sees a real pass (read).
- **`_find_audit_databases`** (`mcp/server.py:823`). Picks a `.db` file to offer at CLI start.
  No write.

## Cross-cutting observations

- **Fix the two measurement blind spots first** (issue #378, direction 1). Five of the ten
  highest scores assessed here are MONITOR on that ground alone, and the `_run_pipeline`
  and `recover_run` classifications both turn on what the testcontainer job covers.
- **Hand-copied "close out" tails are the common shape of the composer risk.** `run_tool_batch`
  (about 17 exits), `_run_composer_turn` (14 audit-persist tails), `_plan_pipeline_inner`
  (seven exhaustion blocks), `_classify_and_budget_turn` (two terminal tails) and
  `_compose_loop` (12 crash raise sites) all repeat one multi-step obligation by hand. In each
  the first decomposition is the same: one helper for the tail, with a test that every exit
  meets the obligation.
- **Growth with history is still present in three places not covered by the 0.8.16 work:**
  `from_records` (whole history per turn), the planner's per-call preparation, and
  `_create_pending`'s read of superseded review history. None was timed.
- **Mutation testing is worth it only after extraction.** The candidates are the type resolver
  in `_check_schema_contracts`, the failure-primacy block in `_compose_loop` and the
  edge-matching block in `build_execution_graph`.

## Limitations

- No test was run and nothing was timed. Every cost statement is read from code and sized by
  inference.
- Coverage is the baseline run only. The 0.8.16 and 0.8.17 trees have no coverage data here,
  so new code in flight (for example the +508 lines in `state.py`) is unassessed, and tests
  added in flight may cover arms listed above.
- Testcontainer and child-process coverage is absent from the baseline. For `recover_run`,
  `read_after` and the progress authority, tests were found by reading. For the `_run_pipeline`
  arms and for cross-operation blob deletion recovery, none was found by name but test bodies
  were not read.
- Some test lookups used the current checkout, which is on an older 0.8.14 branch. Those are
  marked "read on the current checkout" and should be rechecked on the target branch.
- The largest functions were read in sections. `_run_pipeline` `:3811–4380` and
  `WebScrapeTransform.process` `:1476–1540`, `:1570–1600` were read only at the lines coverage
  flagged. `_semantic_validator.py`, `_producer_resolver.py` and `_validation_probe.py` were
  not read.
- Not verified: what the user sees when `from_records` raises; whether the repair gate at `service.py:2362` is
  reachable; whether a re-run of `restore_from_journal` converges after an early journal
  release; what supervises a process stuck in `shutdown`.
- Not known: which of `value_transform`, `web_scrape`, JSON sources, or workflow governance
  production graphs use; what row shapes earlier writers left; whether large-graph work
  goes mainly through the planner or through compose-loop tool edits. Each of these moves the
  ranking of at least one item.
- Classifications are judgements. Items 1–5 rest on code that was read and coverage that was
  measured. Item 6's main failure mode rests on an untimed cost, so its first step is to time
  it.
