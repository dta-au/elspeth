# D2 / adjacent02.17 platform lifecycle compatibility

Read-only follow-up, 2026-11-09. HEAD remeasured:
`5e192342c3fb025fd9a91244bebe437158e9de8e`. Read root `AGENTS.md`,
`d2-design-review.md` and the source/docs listed below. No tests, provider calls,
production/test edits, broad scans or subagents. This is implementation advice,
not a passing-platform or release-support qualification.

Parent subsequently relayed the user's decision: preserve existing platform
support and implement substantive portable process supervision. The alternatives
below explain the compatibility tradeoff; Linux-only or degraded non-Linux
operation is not the selected implementation scope. No dependent implementation
was performed here.

## Conclusion

No already-established project-wide or Composer-MCP Linux-only policy was found
in the targeted support/entry-point sources. The documented deployment profiles
maintain Linux/container **Web deployments**; they do not make native MCP/CLI
Linux-only. Treat unconditional use of the current production watchdog as a
real MCP platform compatibility restriction. The source currently permits
non-Linux startup intentionally, while warning about a narrower parent-death
workaround limitation.

There is no existing production portable watchdog available to substitute for
`ProcessWatchdog`. The test watchdog, logging recovery callback, parent PID
poll, and resource-worker wrappers are not equivalent lifecycle owners. A full
02.17 fix cannot honestly claim existing non-Linux compatibility solely by
wiring the Linux factory and observing Linux tests.

## Support evidence and its actual scope

| Source | Observed contract | Consequence |
|---|---|---|
| `pyproject.toml:1–18` | Python `>=3.12`, Python 3.12/3.13 classifiers; no OS classifier or package platform gate | Not a promise of qualification on every OS, but no existing Linux-only package declaration to cite. |
| `README.md:60–77` | Native source-checkout install requires Python and uv; no Linux-only instruction | Cannot silently replace that general CLI entry posture with a project-wide Linux-only inference. |
| `docs/reference/deployment-platforms.md` support matrix and production contract | Maintained Docker/AWS/ACA/Azure Ubuntu/native Linux profiles; subject is Web deployment artifacts/processes | Strong evidence that the existing Web watchdog fits maintained deployment targets, not that all MCP consumers are Linux. |
| `ARCHITECTURE.md:306–307` | Refers current deployment status to that matrix | Does not extend it into a native MCP OS policy. |
| `docs/architecture/requirements.md` | Compatibility pointer to architecture, configuration, release guarantees/contracts/issues | No remaining OS requirement matrix. Targeted reads of release guarantees and local Web/first-pipeline guides did not establish a Composer Linux-only policy. |
| `composer_mcp/server.py:1088–1131` | Parent-death workaround says macOS/Windows/BSD remain vulnerable, suggests SDK fix or portable parent poll, and explicitly returns on non-Linux | Existing entry consciously continues on those platforms. The workaround is expressly nonessential and is not a supported-platform gate. |
| `CONTRIBUTING.md:62` | Trusted CI runner labels include Linux/X64 | Test-host configuration is not a product-platform compatibility ruling. |

This is a scoped negative finding about these maintained declarations, not a
claim to have searched every historical document or external issue.

## Production owner and portability barriers

`process_watchdog.py` defines the nominal `ProcessWatchdogControl` ABC and one
production implementation, `ProcessWatchdog`; `create_process_watchdog` returns
that implementation unconditionally. At construction it uses `os.pidfd_open`
(`:107`), `signal.pidfd_send_signal` (`:117`), AF_UNIX socketpair and
`subprocess.Popen(pass_fds=...)`. Its independent helper
`process_watchdog_helper.py` additionally uses `/proc/self/fdinfo/<pidfd>` to
verify the reflected target, `os.pipe2`, selector readiness on pidfd, and
`pidfd_send_signal(SIGKILL)` for the deadline. These are substantive Linux
identity/liveness/termination guarantees, not incidental imports to guard.

`ProcessRecovery` depends on that independent owner: acknowledgement precedes
SIGTERM, failed supervision forces SIGKILL, and shutdown/recovery join tasks are
retained. Its `signal.SIGKILL` references are also not a portable Windows
termination abstraction. Merely replacing `pidfd_open` with `os.kill(pid, 0)`
would remove stable target identity and leave helper/platform assumptions.
Ignoring missing APIs, swallowing startup failures, or supplying the nominal
test fake would misrepresent actual recovery ownership.

`async_workers.configure_required_executor_recovery` is a process-global owner
registration, not a watchdog implementation. It accepts a real callback but
provides no process-independent termination itself. The quarantined executor's
observer calls that callback on drain expiry (`required_executor.py:314–323`).
Owner registration must therefore remain substantive on every enabled platform.
The standalone lifecycle belongs around `run_server`; embedded constructors
must continue to use their host's existing owner rather than overwrite globals.

## Existing alternatives inspected

* `tests/fixtures/process_watchdog.py:OwnedTestProcessWatchdog` records signals
  and acknowledgements for assertions; its module explicitly says it has no
  subprocess, pidfd or signal authority. It is not a production fallback.
* The MCP parent-death workaround's suggested `getppid()` polling addresses
  orphaned stdio EOF/busy-spin only. A healthy parent may remain connected while
  a required worker hangs. An event-loop/thread poll in the hung process cannot
  supply the independent escalation and stable target custody required here.
* Landscape `mcp/query_worker.py:RawQueryWorker` owns a query subprocess and
  retains kill/reap cleanup, but explicitly refuses execution on non-Linux
  (`:60–61`) for qualified memory enforcement; its worker entry also refuses
  (`:285–286`). It does not supervise the Composer process or terminate a hung
  shared Python thread, and is no portable owner to reuse.
* PDF rasterizer `worker_initializer` says its resource limits are Linux-only
  and no-op elsewhere (`plugins/infrastructure/rasterize/worker.py:40–48`).
  Resource-limit initialization is not process recovery; this precedent does
  not authorize a no-op Composer recovery path.

## Smallest correct direction

For the already-maintained Linux execution target, reuse the existing real
watchdog/recovery/lifecycle seam from `d2-design-review.md`. Do not expand Web
platform behavior or create an entirely separate executor. This is the narrow
Linux implementation, but it does not by itself settle MCP compatibility.

For a **complete compatibility-preserving 02.17 fix**, the smallest architectural
extension is a real platform backend behind the existing watchdog ownership
boundary, keeping one recovery protocol, completion-witness rules, independent
monitoring and MCP lifespan. It must hold a stable OS process object, observe
actual termination and kill the exact owned target after the deadline. Linux
keeps the current pidfd backend. A non-Linux backend requires actual supported
platform qualification; no existing implementation here supplies it.

One portable design worth evaluating is a supervisor that owns the MCP runner
as its child and retains its `Popen`/OS process handle until reaped. On POSIX,
unreaped child ownership prevents PID reuse; on Windows, termination must use
the retained process handle. The child can inherit MCP stdin/stdout unchanged,
with a separate private recovery channel. That could preserve the public CLI
transport and tool behavior, but it changes process topology, startup/EOF/
cancellation forwarding and handle inheritance, so it is **not** a trivial
fallback to implement during D2. Windows also needs a platform-neutral
graceful/forced termination request in place of direct SIGKILL assumptions.
This paragraph is an implementation option, not a claim that such code exists
or that all POSIX/Windows guarantees have been designed/verified.

If that extension is outside the authorized bounded slice, obtain an explicit
MCP platform decision before declaring 02.17 complete. A new Linux-only stdio
support boundary plus an early actionable unsupported-platform refusal is a
small sound implementation choice, but it is a **new compatibility decision**,
not enforcement of a policy already documented. It must not be inferred from
the Web deployment matrix.

A smaller interim containment can retain non-Linux discovery/edit/session
commands and explicitly mark runtime preflight unavailable, with no shared
worker submitted and no fake recovery callback. This preserves the currently
working methods at the measured HEAD and fails preview closed, analogous to
the codebase's operation-scoped Linux refusal for raw MCP SQL. It does **not**
restore useful non-Linux preview and therefore cannot satisfy full 02.17 or be
reported as feature-compatible completion. It is an explicit product tradeoff,
not a hidden fallback recommendation.

No option requires changing D2's configured-root admission design. Keep that
path work independent while resolving actual process ownership for useful
fresh-process preview. Future verification must use fresh public CLI children
on each claimed platform and independent escalation checks; pytest's autouse
recovery injection cannot establish production initialization or portability.
