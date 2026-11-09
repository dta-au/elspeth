# Portable lifecycle slice 3 — public integration

Prepared 2026-11-09 in the exclusively assigned worktree
`/workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle`.
HEAD remains `8c35a2e2a9092260f15e88525cbe343df5410d4e`. The parent's later queue
change was not copied or rebased into this candidate. No commit, push, agent,
installation, migration, provider request, signing, suppression, Page update,
transform change, session-proposal test change, full suite, or PostgreSQL suite
was performed. Parent owns independent review and combined integration gates.

## Result and review artifact

The actual public `elspeth-composer` executable now creates real standalone
recovery ownership before shared work. Fresh SDK stdio initialization, legitimate
null-source/CSV-output preview, contained CSV preview in singular/plural forms,
patching, immediate escape refusal, legacy path refusal, public export and clean
EOF all pass without pytest recovery injection in the child. Native platform
qualification remains outstanding; this is not a claim of complete cross-platform
D2+adjacent02.17 release qualification.

Complete review diff, including all eight new files and all eight modified files:

- `d2-lifecycle-3-review.diff`: **81,379 bytes**, SHA-256
  `fa0297dbf92d5faeb6a80099d28b216daa7fb282b1c1eaa25b66e1a19c629949`.
- `git diff --check` and complete-artifact `git apply --reverse --check` both
  exited **0**. `git apply --numstat` independently names the exact 16-file set.
- `d2-lifecycle-3-artifact.json`, `d2-lifecycle-3-review-numstat.txt`, and
  `d2-lifecycle-3-artifact-verification.log` retain commands, file inventories,
  exits, bytes, hash and the final source freeze. Nothing is staged.

## Public red and green

The first reproduction ran before production edits at the assigned HEAD, through
`mcp.client.stdio.stdio_client` and `ClientSession` against the actual installed
console executable. The client-side factory wrapper merely retains the actual
SDK-created process object; it does not replace the process, owner, handler,
worker, error framing, or child environment. Both worktree source roots were
exported. The legitimate null source and valid CSV sink authored successfully;
preview returned the production sanitized **`Tool error: AuditIntegrityError`**.
The matching durable audit row records `preview_pipeline`, `plugin_crash` and
`AuditIntegrityError`. The original call graph reaches the absent-registration
check in `run_sync_in_worker`; no recovery registration existed in that fresh
public process. The public framing intentionally does not expose the internal
`Shared executor recovery callback absent` text; it was not monkeypatched to do
so. The client helper exited **1**, and observed the old public process exit **0**.

Exact original command, from the assigned worktree:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
timeout 45 .venv/bin/python tests/helpers/composer_stdio_probe.py \
  /workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-3-red
```

Raw evidence: `d2-lifecycle-3-red.log`, `.exit`, and
`d2-lifecycle-3-red/{receipt.json,stderr.log,scratch/*.events.jsonl}`. The helper
was subsequently extended to add the remaining acceptance controls. Its initial
red is an actual public transport failure, not an intentionally invalid graph.

The same command after integration, with the destination ending in
`d2-lifecycle-3-green`, exited **0**, with a valid preview and actual process
returncode **0**. The later `d2-lifecycle-3-combined` and
`d2-lifecycle-3-topology-green` standalone commands also exited **0**. Final
expanded public acceptance is retained in the frozen pytest receipts below.

An independent settlement negative control temporarily bypassed the new
recovery-generation join loop. Both new old-generation controls then failed
with **`executor futures joined but old recovery observer is still owned`**:
**2 failed, exit 1**. The production loop was restored before all green/frozen
runs. `d2-lifecycle-3-settlement-red.{log,exit}` records the command/result.
This proves that an already-joined executor plus replacement cannot satisfy the
new observer-settlement oracle.

## Actual topology, bootstrap and shutdown

The package console entry executes the one dedicated `portable_supervisor.py`
file using `runpy.run_path(..., run_name="__main__")` in the already-fresh console
interpreter, with deliberate private argv. It does not import the application
server first. The same path runs on every OS. There is exactly one actual private
runner child, launched by retained `Popen`, with inherited original
stdin/stdout/stderr, cwd and ordinary environment. No shell or stdio relay is
introduced. The dedicated owner closes its redundant stdin/stdout copies after
inheritance; stderr remains available for diagnostics.

An initial `os.execv` implementation was replaced, with parent approval, to avoid
depending on unqualified Windows CRT overlay/process-handle semantics. The final
console audit control executes the actual installed entry script, proves that
its observed PID remains the supervisor PID, permits exactly one child spawn,
and refuses application, provider, contracts, MCP, SQLAlchemy or Pydantic imports
before that spawn. Local CPython evidence is in
`d2-lifecycle-3-cpython-topology.txt`: `runpy` executes the file's code in-process;
the Windows `Popen` branch uses its retained `_handle` for `TerminateProcess`.
These are source observations, not native Windows execution evidence.

The runner loads the actual stdlib-only wire file once under its canonical module
identity, without fabricating parent packages or duplicating wire classes. It
then authenticates and consumes both bootstrap variables before application
imports. Runtime imports reuse those same nominal classes. Fresh-child controls
prove no application/provider import before authentication, exact canonical
identity after runtime import, and no nonce/control variables in a real
subsequently spawned descendant environment. The existing retained decoder,
partial prefix, connection-wide count and shutdown intent from slice-2 fix1 are
unchanged and exercised by the private cohort.

`run_standalone` binds the actual serving root, starts real `ProcessRecovery`,
registers its real required-generation callback/events exactly once through the
explicit fresh-process registration seam, creates the catalog, and supplies its
retained coordinator through `run_server` and `_build_stdio_server`. There is no
placeholder-owner submission interval. Help and parse errors use the narrow
no-construction bootstrap completion path and preserve exits 0/2 and public
`elspeth-composer` usage text.

On EOF, cancellation or failure, the root stops serving, sets draining and joins
the independent recovery acknowledgement before any potentially blocked join.
It seals/joins the actual coordinator, joins actual worker shutdown futures and
retained generation observers/drain threads, completes only after settlement,
then joins the owner monitor. Each cleanup action remains retained under repeated
caller cancellation. A caught cancellation or distinct cleanup failure does not
skip later joins. Primary outcomes and cleanup originals are raised separately
or together in a `BaseExceptionGroup`. Failed registration or cleanup withholds
completion; an unresolved join remains awaited under the independent deadline.
Completion is a settlement assertion, not a successful-process-exit assertion.

The old Linux controller-death protection was **moved**, not simply dropped.
Only dedicated supervisor startup installs `CDLL(None)` / `PR_SET_PDEATHSIG`
after its stop-intent handlers and before child spawn. This avoids
`find_library` helper processes. The registration race compares the observed
parent against a locally recorded identity, including reparenting to a non-PID-1
subreaper; that identity never becomes a signal target. Syscall failure remains
visible. Controller death now requests cooperative runner cleanup under an armed
independent deadline. Imported helper APIs and embedded server construction do
not install this process-wide state. Non-Linux uses the shared portable owner
and does not execute the Linux syscall.

## Narrow executor ownership expansion

Parent accepted the evidenced expansion before implementation. Previously,
`shutdown_async_workers` joined current executor futures but did not join a
quarantined generation's actual observer or physical drain thread. Replacement
could also replace `_GENERATION_CUSTODIAN` while the older observer remained
unfinished. The new registry retains only outstanding recovery ownership,
releases successful observers after actual thread exit and result retrieval,
and retains failures/escalations until shutdown reports them. It is not a
completed-generation archive.

The custodian's two optional internal retention/release callbacks and physical
thread-exit settlement supply that ownership seam. Submission, witness,
quarantine/replacement algorithms, budgets and Web lifecycle topology were not
rewritten. Shared shutdown now joins older retained generations, preserves both
caller cancellations and observer/drain failures, and refuses completion after
escalation. The explicit `configure_standalone_executor_recovery` wrapper refuses
an existing owner; the existing host registration API retains its behavior.

A real queue-then-raise executor produces the actual quarantine/replacement.
The tests hold the real observer after its original work, submit through the real
replacement, and prove shutdown remains pending until that old observer exits.
Two cancellation originals and an injected observer failure retain their exact
identities. Fixture isolation now checks every retained old generation and
physical drain thread before resetting a **settled unit harness**; no production
reset or fresh-public-child pytest callback was added.

## Public/process acceptance evidence

`d2-lifecycle-3-process-proofs.json` indexes measured receipts and real exits.
The final SDK receipt is under
`d2-lifecycle-3-supplement-pytest/test_public_executable_real_pr0/receipt.json`.
The final measured normal public topology was supervisor **320433**, runner
**320434**; both were absent after observed SDK process exit **0**. These are
observations of test-owned processes, never signal authority for unrelated PIDs.

| Case | Actual observation |
| --- | --- |
| Original defect positive | Null source and legitimate CSV output; real bounded-worker preview valid, public process exit 0. |
| Combined file positive | Real in-root CSV; singular and plural pipeline forms valid through named `path_allowlist`, `plugin_instantiation`, `graph_structure`, `schema_compatibility`; representative output patch succeeds. |
| Immediate refusal | Outside absolute, traversal and escaping symlink source patches refuse with exact `source_path_policy_invalid`; saved whole private bytes remain identical, including version and options. Existing configured-root cohort retains full public/private mutation and Web tenancy controls. |
| Legacy refusal | Loaded escaped private state stays preserved. The actual named `path_allowlist` check fails; later checks carry `validation.skipped_after_failure` and are not counted as independent failures/verdicts. |
| Export/private | Actual public command strips source, sink and provider storage canaries from YAML and adjacent public state. Saved private options survive export byte-for-byte. Existing bulk sink normalization remains `outputs/PRIVATE-OUTPUT.csv`; no provider preview/request is invoked. |
| Normal completion | Private real lifecycle exit 0 with actual worker/registry settlement; public EOF exit 0 and no surviving owned runner/supervisor. |
| Caller timeout/cancellation | Real registered preview reaches its unchanged five-second budget and returns `Tool error: TimeoutError`. Two later callers cancel. EOF still sees one actual admission and cannot complete early. Release permits real joins; subsequent catchable stop preserves cancellation and actual exit 1. |
| Unresolved work | Current production 30-second grace, no accelerated budget: actual status -9, completion absent, runner reaped. Observed wait after drain was 29.858 s; complete testcase 33.714 s. |
| Quarantine/expiry | Actual poisoned submission and required-generation observer reach the captured ten-second expiry and real recovery callback. After physical release, escalation still withholds completion and yields actual exit 70 with `RequiredGenerationUnavailable`. |
| Partial startup / transport | Actual catalog startup fault and transport-context fault preserve `LookupError` / `OSError`; settled resources complete, process exits 1. |
| Cleanup failure | Real shutdown joins before the precise cleanup fault; original transport `OSError` and cleanup `ArithmeticError` are retained. Completion absent; actual exit 70. |
| Controller death | Actual public controller killed through its retained Popen: controller -9; supervisor uses its Linux stop handler, reaps runner, exits 1. Isolated subreaper observes/reaps supervisor. |
| Authenticated channel loss | Controller kills/reaps its exact SDK-owned public supervisor (-9). Actual runner monitor self-exits 70. Isolated subreaper observes/reaps the adopted runner; controller exits 0. |
| Embedded separation | Fresh child with a real existing `ProcessRecovery` owner and coordinator performs real embedded preview, retains exact callback/events and an open host coordinator, then its host performs cleanup. Fresh missing-owner construction/calls preserve the owned `AuditIntegrityError` failure and do not register an implicit owner. |
| Web regression | Existing pidfd/watchdog, actual Uvicorn fatal-worker, nominal recovery, executor factory/custody, coordinator and Web path controls pass in the focused cohort. |

Hung/destructive cases use separate children and outer deadlines. Signal targets
are retained Popen/SDK-owned processes. The isolated Linux subreaper waits only
known adopted descendants; its fallback first uses exact-child `waitpid` to
establish unreaped custody before signalling. It never runs process-wide
subreaper/prctl changes in pytest. Production does not depend on `/proc`.
The test-only direct-child inventory uses PPid because this kernel omits
`/proc/PID/task/PID/children`; positive and empty-negative controls qualify that
instrument before the final topology claims.

## Exact verification and source freeze

All final argv arrays, cwd, exported `PYTHONPATH`, durations and raw exits are in
`d2-lifecycle-3-final-receipts.json` and
`d2-lifecycle-3-supplement-receipts.json`. Imports resolve to both assigned
worktree roots, measured in `d2-lifecycle-3-final-imports.log`. Pytest additionally
receives both through `-o pythonpath`, `-n 0`, and a lane-private project
`--basetemp`. Process tests were serialized; one focused pytest runner ran at a
time.

Main focused command, from the assigned worktree:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -m pytest \
  tests/unit/composer_mcp \
  tests/unit/web/execution/test_runtime_preflight_coordinator.py \
  tests/unit/web/test_process_watchdog.py tests/unit/web/test_process_recovery.py \
  tests/unit/web/test_async_worker_settlement.py \
  tests/unit/web/test_required_executor_custody.py \
  tests/unit/web/test_required_executor_factory.py \
  tests/unit/web/test_required_executor_completion_identity.py \
  tests/unit/web/test_async_workers.py tests/unit/web/test_paths.py \
  -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" \
  -k 'not production_grace and not real_watchdog_armed_channel_loss' \
  --basetemp=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-3-final-pytest \
  --junitxml=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-3-final.xml
```

| Final check | Measured result |
| --- | --- |
| Focused cohort | exit 0; **600 passed, 2 deselected**, 256.09 s pytest time. |
| Scoped Ruff / format | both exit 0; all 15 changed/new Python paths. |
| Scoped mypy | exit 0; all seven changed/new production paths. |
| Whole contracts/config/census | exit 0; no repin; 2,615 soft occurrences, 416 files, 60 boundary-parsed. |
| Complete review artifact reverse check / whitespace | both exit 0. |
| Final test-only public extensions | exit 0; **2 passed**, 17.08 s; affected public EOF/projection and timeout/release cases only. Helper/test Ruff and formatting both exit 0. |

The two deselections are the previously established slice-2 private full-budget
production-grace and unchanged Linux-helper armed-channel-loss controls. This
run includes a new full-budget real deadline through the actual integrated
standalone lifecycle and current dedicated supervisor. No full/broad backend,
PostgreSQL, provider or hours-long masquerade corpus was rerun.

The 600-test/static run was frozen before/after at **6,221 inputs**, SHA-256
`e318dee33fad1f4fcdadc1bd821409573864ab975877528e3b04d239d1f66795`.
After it, only two test files changed: the SDK helper adds actual public provider
export/private preservation, and the lifecycle test adds the real preview timeout
before its repeated-cancellation/release control. Production and all other inputs
are byte-identical to the 600-test candidate. The affected two cases were rerun,
with helper/test Ruff/format and whitespace under a new complete freeze.
`d2-lifecycle-3-supplement-freeze.json` proves exact membership and the two-file
delta; this is why the unchanged 600-test cohort was not repeated.

Final supplement before/after and review manifests match byte-for-byte:
**6,221 inputs**, SHA-256
`3decb176b74092fbbb291e9a0ce855ad91928a88698015a6cc039491362e167f`.
The canonical symlink-safe freeze script covers tracked bytes/modes/link payloads,
untracked source/test inputs and ignored non-cache source/test inputs without
following symlinks. Evidence: `d2-lifecycle-3-supplement-{before,after}.json`,
`d2-lifecycle-3-review-freeze.json`, and the corresponding logs/receipts.

Diagnostic failures are preserved and are not counted as security/lifecycle red:
initial mypy reported three annotation/coroutine issues, fixed before the final
run; an initial topology probe used the unavailable kernel `task/children` file;
a parent-death helper initially imported a sibling via a missing tests-root path;
and the first provider private-state assertion omitted D2's existing `outputs/`
normalization. Their logs remain under the initial/topology-diagnostic/
parentdeath/projection prefixes. No production policy was weakened for them.

## Complete changed-file review

1. `composer_mcp/__init__.py`: lightweight process-owning console main; same public entry, no application pre-import; lazy embedded create export retained.
2. `composer_mcp/portable_supervisor.py`: dedicated CLI flag, safe redundant-stdio closure, Linux controller-death guard after intent handlers; existing process custody, channel, budgets and status epilogue retained.
3. `composer_mcp/server.py`: explicit coordinator pass-through in stdio builder/transport; unchanged public argument parser extracted; process-owning main bridge; old child-side Linux workaround removed in favor of the supervisor location.
4. `composer_mcp/runner.py` (new): real canonical stdlib wire loading, authenticated bootstrap before runtime, help/error bootstrap witness, private child entry.
5. `composer_mcp/standalone.py` (new): actual root/recovery/strict ownership, retained coordinator and actual cleanup/monitor settlement, truthful witness and original outcomes.
6. `web/async_workers.py`: explicit standalone registration refusal and only outstanding generation-recovery retention/join, preserving existing host registration and worker algorithms.
7. `web/required_executor.py`: optional private lifecycle ownership callbacks and actual physical drain-thread settlement before successful observer release.
8. `docs/guides/composer-mcp-lifecycle.md` (new): topology, loopback requirement, process-owning versus embedded APIs, Linux extra protection and honest platform limits.
9. `tests/conftest.py`: existing explicit unit lifecycle fixture additionally checks/islands its new retained registry only after settlement; no child injection.
10. `tests/fixtures/required_executor.py`: fixture refuses unfinished observer or live physical drain thread even after generation replacement/closure.
11. `tests/unit/composer_mcp/test_server.py`: obsolete workaround patch removed; argument transport pin now uses the extracted parser and embedded transport seam; actual CLI is covered by new subprocess tests.
12. `tests/helpers/composer_stdio_probe.py` (new): actual SDK/public executable original, D2, legacy, provider export/private and exit/topology controls; transparent client process observation only.
13. `tests/helpers/composer_lifecycle_probe.py` (new): precise faults around real standalone/recovery/executor/transport and fresh real/missing embedded ownership; no replacement public handler or fake owner.
14. `tests/helpers/composer_parent_death_probe.py` (new): isolated real controller/supervisor-loss tests, retained process handles and exact adopted-child reaping.
15. `tests/unit/composer_mcp/test_standalone_lifecycle.py` (new): actual public, startup/import, help/status, controller/channel loss, timeout/cancellation/deadline/quarantine and host-separation controls.
16. `tests/unit/web/test_async_worker_settlement.py` (new): actual replacement and withheld old-observer shutdown, repeated cancellation and original failure identity; controlled omission goes red.

## Compatibility and remaining limits

The public console name, arguments/defaults and stdio transport are unchanged.
`main` owns a fresh console process; embedded hosts continue to use `create_server`
or the transport seam with their own registered owner. `create_server` is
unchanged; `_build_stdio_server` and `run_server` add only an optional coordinator
keyword so a host can supply/retain its exact coordinator. The standalone
registration function is internal and deliberately refuses any existing owner.
No process-global reset, Web lifespan replacement, HTTP transport, session-token
filesystem authority or new provider behavior was introduced.

D2 canonical configured-root identity remains distinct from Web session custody.
Existing strict plugin/graph, collision, credential, cache, audit and public-export
contracts remain in force. Relative and contained absolute paths retain the
existing materialization rules; bulk sink `outputs/` normalization is preserved.
Loading legacy private state preserves it, while runtime path admission refuses
an escape. Public export never replaces that private working state.

The public lifecycle now requires ephemeral IPv4 loopback binding; refusal fails
startup, not worker admission without an owner. Nonce authentication does not
claim same-user OS isolation. Linux actual process, stdio, guard, deadline, exit,
reaping and Web regression behavior is measured. Native Windows, macOS and BSD
machines were not available; their existing platform posture is preserved in the
shared production implementation, but native stdio/handle/console/resource
qualification is explicitly outstanding. Pure adapters/no-op guard tests do not
supply native evidence.

Non-Linux abrupt controller loss relies on stdio EOF, as before; portable
supervision still handles its own catchable shutdown and runner recovery. Loss
of the supervisory authority simultaneously with an already-unresponsive runner
cannot be guaranteed recoverable by this portable user-space mechanism. Kernel
unkillable states likewise cannot be called successful cleanup.

Parent retains Astra review, integration of independently reviewed siblings,
combined all-rule/boundary/static corpus adjudication and signing where required,
backend/PostgreSQL release gates, tracker/publication decisions, and native
qualification. Source is frozen for that review; no further edits are planned.

## Review fix round 1 — actual transport-descriptor release

Completed 2026-11-09 after the environment restart, at the same uncommitted
`8c35a2e2a9092260f15e88525cbe343df5410d4e` base. Read both checkout/worktree
AGENTS and CONTRIBUTING (their paired hashes match), integration brief, canonical
portable design, report, independent review and descriptor diagnostic. The
original `d2-lifecycle-3-review.diff` is preserved byte-for-byte at its original
SHA-256 `fa0297dbf92d5faeb6a80099d28b216daa7fb282b1c1eaa25b66e1a19c629949`.
Reconstructed its 16-file source in the lane using the base plus that patch;
`d2-lifecycle-3-fix1-prepare.{py,log}` verifies the only pre-resume delta was the
already-added ownership helper and lifecycle regression. No prior slice, original
600-test run or terminal red was restarted.

The P2 was valid: closing CPython's original standard-stream wrappers leaves
descriptors 0/1 open because their FileIO objects use `closefd=False`. The earlier
report's claim of redundant-copy closure was therefore incomplete. The dedicated
production branch now closes the wrappers first (including stdout flush), then
calls `os.close(0)` and `os.close(1)` after successful child inheritance. Stderr is
untouched. The same production path and one retained-child topology remain on all
OSes; imported/embedded use with `dedicated_cli=False` does not close host stdio.
No relay, reset, new owner, new platform fallback or provider behavior was added.

Only three paths differ from the reviewed candidate:

- `src/elspeth/composer_mcp/portable_supervisor.py`: two descriptor-close calls and
  their ordering/CRT explanation.
- `tests/helpers/composer_stdio_ownership_probe.py`: isolated subprocess control
  executes the actual production dedicated supervisor and authenticates a real
  child. Child reads `ping`, writes `pong`, closes its own inherited descriptors,
  and stays alive on the real owner connection until explicitly released.
- `tests/unit/composer_mcp/test_standalone_lifecycle.py`: asserts the subprocess
  result, actual EOF/loss-of-reader observation and clean eventual status.

The retained terminal `d2-lifecycle-3-fix1-red.{log,exit}` is exit **1**: both
`stdout_eof_while_alive` and `stdin_reader_gone_while_alive` were false, while
`inherited_echo` was `pong` and supervisor alive before/after was true. This is
the expected missing-descriptor-release failure, not a bootstrap or child-stdio
failure. It was already terminal before resume and was not rerun.

The first production-fix green ran from the assigned worktree with both exported
source roots, using the following exact command (logs/exits under the lane):

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -m pytest \
  tests/unit/composer_mcp/test_standalone_lifecycle.py::test_dedicated_supervisor_releases_actual_transport_ends_while_alive \
  -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" \
  --basetemp=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-3-fix1-green-pytest
```

Result: **exit 0; 1 passed in 3.85 s**. Both transport ends are absent while the
supervisor lives before and after the observation; inherited echo is `pong`.
The child writes stderr before and after transport closure and ultimately
completes through the real owner, yielding observed supervisor status **0**.

The bounded serial cohort was:

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-mcp-lifecycle
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -m pytest \
  tests/unit/composer_mcp/test_standalone_lifecycle.py \
  tests/unit/composer_mcp/test_portable_supervisor.py \
  -n 0 -o pythonpath="$PWD/src $PWD/elspeth-lints/src" \
  -k 'not production_grace and not blocked_actual_preflight' \
  --basetemp=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-3-fix1-pytest \
  --junitxml=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-3-fix1.xml
```

Result: **exit 0; 85 passed, 4 deselected in 89.69 s**. This includes actual
installed-entry public SDK/file-preview/EOF, help/status, original-process stdlib
launch, controller/channel-loss, startup/transport/cleanup outcomes and private
supervisor controls. The unchanged private full-budget grace case and three
blocked-preflight cases were deliberately deselected; their prior evidence is
retained. Descriptor release does not alter worker settlement or deadline logic,
so the unchanged 600-test cohort and 30-second proofs were not repeated.

`d2-lifecycle-3-fix1-receipts.json` records every exact argv/cwd/PYTHONPATH, duration
and raw exit. Both import paths resolve to this worktree. Scoped mypy on the
supervisor exits **0** (with existing unused-configuration notes); whole
contracts/config/census exits **0**, retaining 2,615 soft occurrences across
416 files, 60 boundary-parsed; formatting and whitespace exit **0**. The initial
Ruff check exits **1** solely for helper `SIM105` (the expected BrokenPipeError
cleanup used `try/except/pass`). The wrapper command consequently exits **1**;
this is recorded static diagnostic evidence, not a green aggregate.

After that frozen cohort, the helper cleanup alone was changed to
`contextlib.suppress(BrokenPipeError)`. Production, lifecycle tests and all other
inputs remained byte-identical. `d2-lifecycle-3-fix1-final-checks.py` runs the
same new regression with `--basetemp=.../d2-lifecycle-3-fix1-final-pytest` and
`--junitxml=.../d2-lifecycle-3-fix1-final.xml`, then scoped Ruff/format on the three
fix paths and `git diff --check`. All exact argv arrays are in
`d2-lifecycle-3-fix1-final-receipts.json`; every exit is **0**, including the final
regression (**1 passed in 3.18 s**). The 85-test cohort and production mypy/contract
checks were not redundantly repeated for that test-only syntax correction.

Both measured phases were source-frozen using the original symlink-safe script,
covering **6,222 inputs**. Hashes are SHA-256 of canonical manifest JSON excluding
its final newline, matching the script's original convention:

| Manifest pair | Equal before/after | SHA-256 |
| --- | --- | --- |
| `d2-lifecycle-3-fix1-{before,after}.json` | yes | `af698df59e8498e9f626b2bbc3730232a51c60643ed7d07f606cf18cc7b5900f` |
| `d2-lifecycle-3-fix1-final-{before,after}.json` | yes | `4c5e043b97da9c0821c125efb68eef781c0e9fdeeaf9e44edbb2f5cf90e70f89` |

`d2-lifecycle-3-fix1-freeze-comparison.json` proves the only inter-phase delta is
that helper. Final `observation.json`, `status.json` and `stderr.log` reside under
`d2-lifecycle-3-fix1-final-pytest/test_dedicated_supervisor_rele0/`.

Separate review artifacts, generated without staging or overwriting the original:

| Artifact | Scope | Bytes | SHA-256 |
| --- | --- | --- | --- |
| `d2-lifecycle-3-fix1.diff` | three-path delta versus reviewed candidate | 7,918 | `0cbb975bcd23d7b74d84e624bdfd23e7e4f7def3d6ba601f4334fab4111aa1bb` |
| `d2-lifecycle-3-fix1-full.diff` | complete 17-file patch versus base | 87,824 | `530cd979f8bb0d647623d0390205cb3f12da42aa2df068443c96fb1b426053d9` |

`d2-lifecycle-3-fix1-artifacts.json` and artifact-verification log retain exact
commands, inventories, hashes, numstats and successful reverse/whitespace checks
for both artifacts (all exit **0**). The fix artifact has only five added
production lines: three explanatory comments and two descriptor-close calls.

This establishes actual Linux descriptor and inherited-stdio behavior on CPython
3.12.14. Intended Windows CRT-handle release follows `os.close`; native Windows
handle/console qualification, and native macOS/BSD qualification, remain explicitly
outstanding. No native success is inferred from adapters or source inspection.
No agents, commits, installs, provider calls, broad scans/suites, PostgreSQL runs,
parent merge or release claim occurred in this repair. Frozen for parent's Astra
scoped rereview; all previously listed combined-release/operator/native gates
remain with the parent.

Final handoff re-freeze `d2-lifecycle-3-fix1-handoff-freeze.json` matches the final
post-test manifest byte-for-byte (freeze, `cmp` and `git diff --check` exit **0**).
`patch --dry-run --batch -p1 -d d2-lifecycle-3-fix1-reviewed-source -i
/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d2-lifecycle-3-fix1.diff`
also exits **0**, confirming the fix applies forward to reconstructed reviewed
source (`d2-lifecycle-3-fix1-forward-check.{log,exit}`). Final JUnit reinspection
confirms 85 plus one tests, zero failures/errors/skips; final status is recorded
in `d2-lifecycle-3-fix1-final-status.txt`, with nothing staged.
