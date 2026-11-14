# Python 3.14 `asyncio.shield` diagnostic: remediation options

Read-only analysis, worktree `.claude/worktrees/composer-0.8.12`, branch `python-3.14-only`, HEAD `55fd7daa`.
Interpreter measured: CPython 3.14.6 (`.venv-314`). Note: `.venv` in this worktree is also 3.14.6 now, so
3.13 behaviour below is from reading the stdlib source and existing test comments, not re-measured here.
Probe scripts are in the session scratchpad (not in the repo); nothing tracked was edited.

## Summary

- Recommend **option C = A as the control, no mass replacement**: one sanitising loop exception handler,
  installed by a shared helper at the three places ELSPETH owns a loop, covering *every* asyncio loop
  diagnostic that carries an exception (not only the shield message). Leave the 59 call sites alone.
- Option B alone does **not** close the leak. Measured: with the private `_shield`, a cancelled awaiter plus a
  late task failure that nobody else retrieves still logs `Task exception was never retrieved` with the message
  and traceback, through the same default handler. B only moves the leak from deterministic to GC-time at
  those sites; it is clean only where the task outcome is retrieved elsewhere.
- At most sites the 3.14 log is a **duplicate** of a failure the code already joins and re-raises: 32 of 59
  sites retrieve the task's `result()`/`exception()` in the same function, and an existing test already
  records the duplicate.
- B has real gate cost: 16 signed tier-model allowlist entries drift for certain, 4 AST/snapshot test helpers
  pin the literal `asyncio.shield`, and 11 `monkeypatch.setattr(asyncio, "shield", ...)` sites in 7 test files
  would silently stop intercepting.
- Main risk of the recommendation: a loop handler is per-loop and replaceable. A loop ELSPETH does not start
  (tests, a future `asyncio.run`) or a library that calls `set_exception_handler` (litellm does when
  `JSON_LOGS` is set) falls back to content-bearing logs without any error.

## Evidence

### 1. What 3.14 does

`asyncio/tasks.py` of the uv-managed CPython 3.14.6
(the `.venv-314` base interpreter; the venv itself has no `asyncio/` copy):

- `_log_on_exception` (line ~913): builds `{"message": "<Cls> exception in shielded future", "exception", "future"}`
  and calls `fut._loop.call_exception_handler`. It calls `fut.exception()`, which also marks the exception retrieved.
- `shield._outer_done_callback`: when the outer future finishes while the inner is not done, it removes the
  relay callback and adds `_log_on_exception` to the inner (once per inner, deduplicated).
- `base_events.default_exception_handler` (line 1837): logs `message`, then `future: <repr>` (the task repr
  includes the exception repr), with `exc_info` — so message and traceback both reach the `asyncio` logger.
- `src/elspeth/core/logging.py:178-198`: `configure_logging` puts a `ProcessorFormatter` handler on the root
  logger, so the `asyncio` logger's record is rendered to stdout (JSON renderer runs `format_exc_info`).

Probe result on 3.14.6 (inner task raises `ValueError("SECRET-CONTENT")` after its only awaiter is cancelled;
identical under stdlib asyncio and uvloop 0.22.1):

| shield used | loop handler | task outcome retrieved elsewhere | secret in `asyncio` log | first log line |
|---|---|---|---|---|
| `asyncio.shield` | default | no | yes | `ValueError exception in shielded future` |
| `asyncio.shield` | default | yes | yes | `ValueError exception in shielded future` |
| `asyncio.shield` | class-only handler | no | no | `shielded task failed after awaiter cancelled exc_class=ValueError` |
| private `_shield` | default | no | **yes** | `Task exception was never retrieved` |
| private `_shield` | default | yes | no | nothing |
| private `_shield` | handler matching only the shield message | no | **yes** | `Task exception was never retrieved` |

Rows 4 and 6 are the key finding: a handler that recognises only the shield message, and a helper with 3.13
semantics, both leave the older "never retrieved" path open. That path exists on 3.13 as well.

### 2. Where ELSPETH runs event loops

All 59 `asyncio.shield(` sites are under `src/elspeth/web` (51), `src/elspeth/mcp` (3) and
`src/elspeth/composer_mcp` (5). Measured: `grep -rn 'asyncio\.shield(' src | wc -l` = 59, 26 files; the AST
walk agrees (59 calls, 49 distinct enclosing functions).

Loop owners in `src/` (grep for `asyncio.run(`, `uvicorn.run`, `new_event_loop`, `Runner`, `anyio.run`,
`run_until_complete`; no `new_event_loop`, no `anyio.run`, no blocking portal in `src/`):

| Owner | Site | Reaches shield code |
|---|---|---|
| Web app | `src/elspeth/cli.py:5288` `uvicorn.run("elspeth.web.app:create_app", factory=True)`; loop is uvloop (`uvicorn[standard]`, `pyproject.toml:178`) | yes, 51 sites. Install point: `lifespan` at `src/elspeth/web/app.py:625` (already calls `asyncio.get_running_loop()` at :634) |
| Landscape MCP | `src/elspeth/mcp/server.py:1029` `asyncio.run(run_server(...))` | yes, `mcp/query_worker.py` |
| Composer MCP | `src/elspeth/composer_mcp/runner.py:58` `asyncio.run(run_standalone(...))` | yes, `portable_control.py`, `standalone.py`, and the shared preflight coordinator |
| AWS acceptance drivers | `web/aws_ecs_acceptance.py:564`, `web/_aws_ecs_acceptance/operator_telemetry.py:744` | not established; short-lived operator tools |
| Worker threads | `web/execution/service.py:1739, 2979, 3015, 5469`, `web/execution/recovery.py:256` use `run_coroutine_threadsafe` back onto the main loop | no second loop; threads do not own loops |
| `elspeth-lints` | `core/judge.py:1694`, `mcp/server.py:1085`, `mcp/codex_judge_tools.py:408` | no shield sites |

Can the handler be replaced or bypassed? `grep -rln 'set_exception_handler('` over `.venv-314` site-packages
finds three packages:

- `anyio/_backends/_asyncio.py:2279`: `TestRunner.__enter__` only (the anyio pytest plugin). Not used by
  `anyio.run` or Starlette's `TestClient` portal. 41 lines in `tests/` reference `mark.anyio`/`anyio_backend`;
  a lifespan that installs a handler without chaining would replace anyio's test collector on that loop.
- `litellm/_logging.py:576`: `_setup_json_exception_handlers` calls
  `asyncio.get_event_loop().set_exception_handler(...)` when `JSON_LOGS` is truthy (line 582) or JSON logging
  is turned on later (line 787). Its handler logs `str(exception)` with `exc_info`. 16 import sites of litellm
  in `src/elspeth`. At import there is normally no running loop, so on 3.14 the call raises and is swallowed,
  but a call from inside the running loop would replace the application handler.
- `uvloop/_testbase.py`: test base only.

uvicorn 0.46.0, starlette 1.3.1, fastapi 0.136.1, mcp 1.30.0, httpx 0.28.1 never call `set_exception_handler`
(uvicorn only calls `call_exception_handler`, `_compat.py:85`). So a handler set in the lifespan survives in
the normal web process. Not covered by entry-point installation: pytest-asyncio loops, app instances that never
run the lifespan, and any new `asyncio.run` added later.

### 3. Site sample (what is shielded, can it carry content, is the failure seen elsewhere)

AST heuristic over all 59 sites (same-function check for `<arg>.result()` / `<arg>.exception()`):
32 join-and-retrieve in the same function, 2 drain loops that hand the task to a join helper, 25 others
(single shield, task passed on or shared). The 32 is a lower bound on "already observed".

| Site | Shielded work | Content risk | Failure observed elsewhere? |
|---|---|---|---|
| `web/composer/service.py:204` `_await_tool_turn_with_deferred_cancellation` | composer tool dispatch + persistence | high (tool args, SQL parameters) | yes, loop re-shields and returns/raises the task outcome |
| `web/composer/pipeline_planner.py:3066/3075` `_await_custody_settlement` | custody settlement write | high | yes, `raise cancellation from failure` |
| `web/composer/pipeline_planner.py:3089/3099` `_settle_lifecycle` | lifecycle `on_settled` callback | medium | yes; `tests/unit/web/composer/test_pipeline_planner.py:6358-6373` already records that 3.14 emits the shield diagnostic "even after its inner exception has been retrieved and chained" |
| `web/composer/prompt_guard.py:398` `_join_audit` | prompt-guard audit SQL | high (prompt text in parameters) | yes, `task.result()` re-raised, grouped with cancellations |
| `web/composer/provider_quota.py:145` `ProviderCallCustody._join` | provider attempt admission/settlement SQL | medium | yes, re-raised or wrapped as `ComposerOwnedSettlementFailure` |
| `web/composer/provider_quota.py:492/507` `admit_provider_attempt` | admission, undispatched disposition | medium | yes, via `_join_after_cancellation` |
| `web/sessions/service.py:2701-2727` `archive_session.run_owned_phase` | lease-owned archive phases (blob moves, SQL) | medium (paths, SQL) | yes, `raise phase_error from cancellation` |
| `web/sessions/service.py:9013`, `9104`, `9147` | post-commit projection worker, composer terminal SQL | high | yes, `worker.result()` / `child.result()` |
| `web/process_recovery.py:82`, `web/async_workers.py:609`, `composer_mcp/standalone.py:53` | shutdown/recovery joins | low | yes, `task.result()` |
| `mcp/query_worker.py:98/125/149` | subprocess spawn and cleanup | low to medium (OS errors) | yes; the module already retrieves late failures on purpose (`_consume_cleanup_error`, :139-145: "without emitting arbitrary asyncio traceback diagnostics") |
| `web/readiness.py:633` `get_or_compute` | shared readiness compute | medium (probe errors, URLs) | yes, class-only: `readiness_compute_failed_without_waiter` at :616 |
| `web/execution/validation_memo.py:173` | joiner on another caller's memo future | n/a | the owner in this module only calls `set_result`; no exception is set on the future in the code read, so the shield log should not fire here |
| `composer_mcp/portable_control.py:123-171` | watchdog recovery/completion exchange, reader task | low (protocol errors) | shared stored tasks; later awaiters observe. If the only awaiter is cancelled, the 3.14 log (or the GC-time log) is the only report |

Conclusion: for the join-and-retrieve majority the new log is a second, content-bearing copy of a failure the
caller already receives. For the few shared-task sites it can be the only report, so suppressing it outright
would lose signal; a class-only line keeps it.

### 4. Whole-tree gate churn for option B

- **Signed tier-model allowlist** (`config/cicd/enforce_tier_model/*.yaml`, 1276 entries, 1275 signed).
  `scope_fingerprint` hashes the AST of the innermost enclosing def/class
  (`elspeth-lints/.../tier_model/scope_fingerprint.py:41-67`), so changing `asyncio.shield(x)` to `shield(x)`
  changes it for every entry bound to that scope or an enclosing one. Matching entry key symbol paths against
  scopes that contain a shield call: 100 entries live in the 26 files; **16 drift for certain**
  (lifecycle.py 7, sessions/service.py 6, pipeline_planner.py 2, proposals.py 1), 71 are in clean scopes,
  13 name a symbol that no longer exists in this tree (already stale, unrelated). Adding an import line does
  not add drift: a module-level `ast_path` prefix shift is rescued when the scope fingerprint matches
  (`core/allowlist.py:654-720`), and none of the 100 entries is module-level. The 16 need an operator
  re-judge and re-sign; agents cannot do that ([O1]).
- **AST/snapshot helpers pinning the literal name**: `tests/helpers/web_reserve_provider_mint_contract.py:9`
  (full unparsed source of `ProviderCallCustody`, contains `await asyncio.shield(task)`),
  `tests/helpers/web_reserve_family_contracts.py:969` (AST template `asyncio.shield(compare_and_swap_tasks[0])`),
  `tests/helpers/web_reserve_route_helper.py:399` (counts `("asyncio","shield")` calls, must equal 1),
  `tests/unit/architecture/test_web_landscape_mutation_fencing.py:5110` (mutation pair
  `"await asyncio.shield(task)" -> "await task"`). All four go red or need regenerating.
- **Tests that patch `asyncio.shield`**: 11 `monkeypatch.setattr(asyncio, "shield", ...)` call sites in 7
  files (for example `test_worker_owned_sql_cancellations.py:113,209`, `test_freeform_route_custody.py:144,772`,
  `test_pipeline_rejection_callers.py:323,919`, `test_composer_adoption_recovery_native.py:107`). They inject
  cancellation at the shield point. After B they would no longer intercept; some would fail, and any that only
  observe could pass without exercising the path.
- No dynamic-attribute or masquerade baseline impact was found for B (no `getattr` added). I did not run the
  whole-tree gates; these are static counts.

### 5. Behavioural subtleties

Private `_shield` (`src/elspeth/web/execution/runtime_preflight.py:52-86`) versus `asyncio.shield`:

- Accepts `asyncio.Task` only. `validation_memo.py:173` shields a plain `Future`; stdlib also accepts
  coroutines. A shared helper needs a wider signature.
- No `future_add_to_awaited_by` bookkeeping, so the 3.14 call-graph tools (`asyncio.print_call_graph`,
  `python -m asyncio pstree`) lose the awaiter edge. Diagnostics only.
- Done-task fast path and cancellation relay match the stdlib.
- When the outer is cancelled it removes its relay and retrieves nothing, so an otherwise unobserved inner
  failure is reported at garbage collection as "never retrieved" (measured, section 1). It is safe in
  `runtime_preflight` only because that coordinator retrieves every outcome itself.
- A copy of stdlib semantics must be re-checked on each CPython release.

Global handler:

- It sees every loop diagnostic: "Task exception was never retrieved", "Exception in callback ...",
  transport and SSL errors, uvicorn's `_compat.py:85`. Delegating the non-shield ones to the default handler
  keeps their content in the log; sanitising all of them drops tracebacks for real bugs. A middle course is
  class name plus traceback frame locations (file, line, function) without the exception message.
- The `message` string is stdlib wording, not an API. Matching `endswith("exception in shielded future")`
  can break silently on a CPython change; sanitising every context that has an `exception` key avoids
  depending on it.
- Must chain to `loop.get_exception_handler()` when one is already present (anyio `TestRunner`), and tests
  that install their own handler (`test_pipeline_planner.py:6338`, `tests/unit/web/test_async_workers.py`)
  are unaffected because they set and restore per test.
- Output should go through the project's structlog logger with an `exc_class=` field (83 existing
  `exc_class=` uses in `src/elspeth`), not through the `asyncio` logger.

### 6. Test strategy

Option A / C:

- Unit test of the handler: drive the section 1 scenario on a loop with the handler installed; assert the
  secret string is absent from all captured records and one record carries `exc_class`.
- Negative control in the same test module: same scenario with the handler removed must show the secret in
  the `asyncio` logger on 3.14. If it does not, the instrument is not measuring the leak.
- Second scenario for "Task exception was never retrieved" (drop the task, `gc.collect()`), same two assertions.
- Coverage test per loop owner: inside a `TestClient` lifespan, and inside the two MCP entry coroutines,
  assert `loop.get_exception_handler()` is the project handler. Negative control: an app built without the
  lifespan reports `None`.
- Run under both stdlib asyncio and uvloop, since production uses uvloop.

Option B:

- Parity tests of the helper against `asyncio.shield` (done task, inner cancel, outer cancel, result, exception).
- A gate that bans `asyncio.shield` in `src/elspeth` (architecture test or lint rule); negative control is a
  fixture file containing the call that must turn the gate red.
- The "never retrieved" scenario must be tested per site class, because the helper does not cover it.
- Rework the 11 monkeypatch sites and 4 pinned helpers, then operator re-sign of 16 entries.

## Options

| | A: loop exception handler | B: shared `_shield`, replace 59 sites | C (recommended): A, sanitising all exception contexts, shared installer, no mass replacement |
|---|---|---|---|
| Closes 3.14 shield log | yes, on loops where installed | yes, at replaced sites | yes, on loops where installed |
| Closes "never retrieved" content log | only if written to sanitise it | no (measured) | yes |
| Keeps a signal when the log is the only report | yes, class-only line | no | yes, class-only line |
| Production code touched | 3 entry points + 1 helper | 26 files, 49 functions | 3 entry points + 1 helper |
| Signed allowlist drift | none expected (new code only) | 16 entries certain | none expected |
| Pinned tests affected | none found | 4 helpers, 11 monkeypatch sites in 7 files | none found |
| Covers third-party shield use / future call sites | yes | no, needs a ban gate | yes |
| Failure mode | silent if a loop lacks the handler or a library replaces it | silent GC-time leak at unobserved sites; drift from stdlib | same as A; less traceback detail for real bugs |
| Depends on stdlib message text | yes if it matches the message | no | no |

## Recommendation

Adopt C. Write one handler that reports any loop diagnostic carrying an exception as class-only (optionally
with frame locations), passes exception-free contexts to the default handler, and chains to a pre-existing
handler. Install it through one helper at `web/app.py` lifespan, `mcp/server.py:1029` and
`composer_mcp/runner.py:58`. Keep the existing `_shield` in `runtime_preflight.py` or revert it in a later
change; do not replicate it. This is a decision for the developer, since it changes what operators see for
genuine unretrieved-exception bugs.

Main risk: coverage is per loop and unenforced. A loop without the handler, or litellm replacing it under
`JSON_LOGS`, restores content-bearing logs with no error. Mitigate with the per-entry-point coverage tests
above and, if wanted, a readiness check that the running loop's handler is still the project's.

Not done: no whole-tree gate, lint, or suite was run; the 3.13 comparison was not re-measured; the
`validation_memo` and AWS acceptance-driver judgements are from reading the code only.
