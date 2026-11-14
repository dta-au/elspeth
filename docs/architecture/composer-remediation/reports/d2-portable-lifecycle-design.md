# D2 / adjacent02.17 portable lifecycle design

Read-only design review, 2026-11-09. HEAD measured with `git rev-parse HEAD`:
`5e192342c3fb025fd9a91244bebe437158e9de8e`. Read root `AGENTS.md`, canonical lane
`d2-design-review.md`, `d2-platform-lifecycle-map.md`, production watchdog,
helper, codec, recovery and executor interfaces. No production/test edits,
tests, providers or subagents. This file is coordination material, not a claim
of implementation or native-platform qualification.

## Decision and recommendation

The user chose to preserve the current platform posture and implement real
portable supervision. Linux-only startup and a non-Linux no-op/degraded-preview
fallback are therefore not options. The platform map correctly distinguishes
maintained Linux Web deployments from generally available native Composer MCP.
The targeted declarations do not establish a separately qualified OS matrix;
the package requires Python >=3.12 and the MCP source explicitly continues on
macOS, Windows and BSD when skipping its Linux-only orphan workaround.

The smallest sound *portable ownership model* is an independent parent
supervisor that launches and exclusively owns the actual stdio runner as its
child. Prefer the same supervisor path for MCP on Linux and non-Linux: Linux
then exercises production portable control/ownership code, avoiding an entirely
untested non-Linux branch. Keep the existing Linux Web watchdog unchanged except
for a small semantic termination interface shared with `ProcessRecovery`.

Public `elspeth-composer` remains the same command and stdio MCP transport. Its
launcher becomes the supervisor; a private runner inherits the original
stdin/stdout/stderr and executes the existing server. The supervisor processes
only a bounded private recovery protocol, never MCP messages or tool content.
Do not relay or buffer MCP stdout through Python supervisor threads. Do not
create HTTP, host Web, or alter D2 path authority.

## Exact process lifetime authority

The supervisor obtains the target from its own `Popen`, not a PID supplied in
an environment variable or protocol frame. HELLO reflects that PID and the
per-launch nonce and must match the actual child. The child verifies its own
PID against its issued identity. Completion witnesses remain nominal and bound
to that exact target/nonce.

* **Windows:** retain `Popen` and its process handle until observed exit/reap.
  Use its `kill()` for forced termination, which CPython implements with
  `TerminateProcess(self._handle, ...)`; never reopen by PID. `terminate()` is
  also forced on Windows, so it is NOT the graceful phase.
* **POSIX (Linux/macOS/BSD):** the owned child cannot have its PID reused while
  alive or unreaped. A single supervisor loop exclusively owns poll/wait/signal;
  after it observes/reaps exit it never signals again. No waiter thread,
  signal-handler `waitpid`, `waitpid(-1)`, competing library reaper or process
  group kill is permitted. Install ordinary `SIGCHLD` handling before spawn
  rather than inheriting `SIG_IGN`/auto-reap behavior. Signal handlers only mark
  an intent for the main supervisor loop; they do not race termination/reaping.
  Retain the `Popen` strongly until finished. Use `Popen.kill()` while still
  owned. Do not substitute PID polling plus a timestamp check.

This POSIX single-owner restriction is substantive. The local Python 3.12
`subprocess.py` inspected during review explicitly warns that `send_signal`
can race another reaper between checking `returncode` and `kill`. Its Windows
branch uses the retained process handle. This was source inspection, not a
platform test. A dedicated stdlib-only supervisor is what makes the ownership
restriction enforceable; do not run it inside an arbitrary already-hosted app.

## Smallest portable private channel

Parent has accepted this bounded channel choice within the authorized portable
scope. Selected baseline: one bounded, authenticated, IPv4 loopback-only TCP control
connection, using the existing binary watchdog codec and a fresh random 256-bit
nonce. Bind `127.0.0.1:0`, never wildcard or a configured host; hold the listener
before spawning; pass the selected address and nonce only in the child's
bootstrap environment. Remove bootstrap variables in the runner before loading
catalog/plugins or creating further subprocesses. Never print or persist the
nonce. Close the listener after one authenticated child handshake. No discovery
endpoint, HTTP stack or user-controlled bind settings are needed.

The nonce authenticates the private connection; it does NOT supply process
lifetime authority. The supervisor's owned child does that. A peer-provided PID
never changes the kill target. Ordinary local processes lacking the nonce must
not authenticate. Same-user code already able to read/manipulate the child's
environment/process is outside this channel's isolation claim; do not describe
loopback + nonce as OS-isolated descriptor custody.

Bound connect/HELLO/ack deadlines, unauthenticated connection attempts and all
buffers. Slow partial frames and floods must not postpone the independent
deadline or grow memory/threads. Use nonblocking sockets and a selector loop
with a short child-status poll interval. Selectors on Windows accept sockets;
do not register process handles, anonymous pipes or pidfds there. Reject wrong
identity/frames and fail startup closed if the valid child cannot establish
ownership within the fixed bootstrap budget. Bind/connect failure is an
actionable startup error, never permission to submit workers without an owner.

This makes loopback socket binding a new runtime environmental requirement,
which should be documented. It is not an OS restriction and is not a network
tool-serving capability. If local listeners are unacceptable to the project,
use an inherited private socket on POSIX (`pass_fds`) and a separately designed
Windows bootstrap using `socket.share(child_pid)` / `socket.fromshare` over a
bounded inherited native handle. That alternative requires explicit Windows
handle inheritance, bootstrap I/O deadline and cleanup work; do not pretend
POSIX `pass_fds` or raw fd numbers are portable. Avoid selecting both channel
implementations merely for abstraction completeness.

## Keep recovery semantics, remove the Unix-signal assumption

`ProcessRecovery` currently calls `request_signal(SIGTERM/SIGKILL)` at lines
38/45/48. Referencing SIGKILL is itself invalid on Windows. Introduce two
semantic owner operations: request graceful termination and force termination.
Use them in those three recovery sites. The existing Linux owner maps them to
its existing pidfd SIGTERM/SIGKILL operations and may retain `request_signal`
as an internal/compatibility helper. Update the nominal owned test fake to
record the same existing Linux signal evidence. This does not require changing
Web lifecycle, deployment profiles or executor recovery policy.

The MCP child-side `ProcessWatchdogControl` implementation owns the authenticated
supervisor channel and root serving task:

* `assert_watching` requires completed handshake and live monitor ownership.
* `begin_recovery` obtains the supervisor's immutable grace receipt before the
  graceful termination request. Repeated calls return the exact first receipt.
* Graceful termination sets draining and cancels/stops the owned serving task
  exactly once, initiating the existing MCP cleanup sequence. This is a
  cooperative request, not a claim a running Python thread stopped. It needs
  no Unix signal on any platform.
* Force termination acts on the current child itself (`os._exit` with a fixed
  nonzero code is appropriate for the last-resort path). It cannot target a
  reused foreign PID. The independent supervisor still retains and reaps the
  real child. If the child's event loop is hung and cannot invoke this path,
  the supervisor performs forced termination using its process authority.
* Unexpected supervisor channel loss triggers the existing monitor's forced
  child termination; failed BEGIN_RECOVERY/ack likewise fails closed. There is
  no log-only callback or unsupported-platform silent path.

Keep `ProcessRecovery`'s actual required-generation callback, not a replacement
that just asks the root task to stop. `required_generation_expired` must still
record the observation, mark draining, obtain independent recovery acknowledgement
and request termination. Ordinary executor quarantine/replacement stays intact.

## Supervisor state machine and deadlines

Use a small explicit state machine scoped to this CLI, not a general process
manager: bootstrapping, watching, draining, completion acknowledged, child
exited. The last state is terminal and prohibits further signals.

Reuse the existing protocol's bounded frames, nonce/target checks, closed
message/reason enums and `GRACE_NS` (30 seconds). Arm once on BEGIN_RECOVERY,
authenticated channel failure, parent-side shutdown request or supervisor
protocol failure after ownership. Never extend the first deadline on repeated
frames, acknowledgements, client activity or partial data. Preserve the separate
required-generation drain budget (currently 10 seconds) before its callback;
normal shutdown arms the process grace before joins begin. Compare deadlines
in the supervisor's own monotonic clock; child receipts are equality-checked
protocol evidence, not a cross-process clock scheduling primitive.

At deadline force-kill the exact still-owned child and then reap it. Do not wait
forever in `wait()` without enforcing the deadline. Termination-request failure
must become a visible supervisor failure, never clean completion. Kernel-level
unkillable process states cannot be solved by Python and are not a successful
recovery outcome.

Add at most one private `SHUTDOWN_REQUEST` message for supervisor-to-child
graceful shutdown (closed schema and identity validation). This handles parent
SIGINT/SIGTERM or Windows console interruption without calling Windows
`Popen.terminate()` prematurely. The supervisor arms its own deadline before
sending. The child may acknowledge/request BEGIN_RECOVERY idempotently and
cancels its root serving task. On POSIX, put the child in a separate process
session/group so terminal signals do not independently bypass owner cleanup;
on Windows use the appropriate new-process-group creation flag and cooperative
channel shutdown instead of relying on console CTRL events. Normal stdin EOF
continues directly to the child and starts the child's ordinary teardown.

Do not let a receive monitor and `_exchange` independently consume socket bytes.
One child-side asyncio reader task routes the small fixed set of replies and
the shutdown request. One in-flight exchange/retained response future suffices;
no generic RPC framework is needed. Repeated cancellation must not orphan that
exchange or accidentally treat its late ack as an unsolicited-frame failure.
The independent parent uses a synchronous nonblocking selector loop, not the
child's executor or asyncio event loop.

COMPLETE is legal only for the actual lifecycle's completion witness (or the
narrow no-side-effects bootstrap witness), after required task/executor joins.
After DISARM_ACK the launcher still owns and reaps the child, returning its
actual status. Define a short, fixed post-completion process-exit allowance so
an atexit hang cannot strand the launcher indefinitely; never extend an earlier
armed deadline. A completion acknowledgement is not proof the OS process exited.
Any such forced post-completion exit remains a failure status.

## Lifecycle integration and ownership boundaries

The public launcher should load only the lightweight supervisor before spawning;
the child imports the current server/catalog. A private runner entry must refuse
to start workers without an authenticated owner; a private command-line switch
or environment variable alone is not lifecycle authority. Preserve public CLI
arguments/help, original cwd, environment apart from bootstrap variables, exit
status and stdout cleanliness. The supervisor never reads MCP stdin or writes
MCP stdout. Close its redundant stdio pipe copies when safe so it cannot keep
client streams alive independently of the runner; keep diagnostic output on
stderr and no secrets in diagnostics.

The child lifespan configures the shared executor once, before any possible
submission, with its own draining/unavailable events and real recovery callback.
No second owner may replace a live registration. Embedded `create_server` /
`_build_stdio_server` remain host-owned and must not launch a supervisor or
reconfigure Web globals. Keep direct embedded ownership explicit in API/tests;
do not reset executor internals to accommodate pytest's autouse fixture.

On EOF, external stop, transport failure, partial startup and cancellation:
stop admission; mark draining/arm supervision; seal and join the retained
preflight coordinator; join actual workers with `shutdown_async_workers`;
settle required recovery observers/monitor exchanges; then complete only if
the owned resources actually settled without a lost-generation condition.
Retain original cancellation/failure and distinct cleanup errors. A caught
CancelledError must not skip remaining cleanup. On cleanup failure or unresolved
work withhold completion and let the independent deadline enforce termination.

The coordinator close/join seam identified in the previous design remains
necessary: shielding means timed-out preview tasks survive their caller.
Executor futures and preflight asyncio tasks are separate objects to settle.
No `asyncio.all_tasks()` cleanup sweep or new unbounded default-executor pool.

Abrupt external destruction of both supervisory authority and an already-hung
child cannot be guaranteed recoverable by this portable user-space design.
The existing Linux helper also depends on a responsive target monitor when the
helper itself dies. State that limit honestly; do not claim portable kernel
kill-on-parent-death semantics. Catchable supervisor shutdown and protocol EOF
must be handled and tested, and ordinary healthy-child supervisor loss must
force the child to exit. Do not silently treat a killed supervisor as normal EOF.

## Feasible verification and honest platform limits

| Layer | Linux-host evidence to obtain during implementation | Native-platform evidence still required |
|---|---|---|
| Closed codec/state machine | Fragmentation, oversized/count-limited messages, wrong nonce/PID/direction, duplicate recovery receipt, first deadline never extended, child-exited terminal state | Same state machine is shared; no claim of OS qualification from mocks |
| Public SDK stdio child | Real command initialize/file authoring/preview/export/EOF, real supervisor/runner, no pytest callback injection or provider network; stdout remains MCP only | Windows and macOS public command smoke with actual inherited handles/stdio and event loop |
| Ownership | Actual POSIX child kill/reap; exclusive polling, inherited SIGCHLD reset; no signal after exit; never target a frame-supplied PID | Windows actual retained-handle termination; macOS/BSD actual POSIX child retention and signals |
| Fault recovery | Actual child required-worker hang and drain expiry; unresponsive event loop; supervisor deadline kills/reaps; blocked cleanup, EOF, repeated SIGINT/SIGTERM; each test isolated under outer timeout | Windows console/group behavior, child cancellation and TerminateProcess exit; native BSD/macOS signal semantics |
| Authentication/startup | Bad peer/partial frame flood cannot starve deadline; missing bootstrap, connection failure, child pre-handshake crash; no worker submitted before WATCHING_ACK | Windows firewall/loopback policy refusal diagnosed; native handle/stdio launch behavior |
| Cleanup/cancellation | Repeated task cancellation, timeout-held preflight admission, coordinator seal/join, quarantine observer completion, no early witness; actual process/helper absence after exit | Platform-specific resource handles, TCP close behavior and nonzero forced termination status |
| Existing Web | Existing recovery/watchdog tests retain pidfd ordering and SIGTERM/SIGKILL evidence through semantic adapter; no Web process topology change | No new non-Linux Web support claim |

Use deterministic fake OS/clock seams for deadline ordering, but also isolated
real Linux subprocess faults through the same portable production supervisor.
Do not create a monkeypatched-`sys.platform` test and call it Windows coverage.
Use nominal owned fixtures only at unit seams, never as a public child owner.
At least one actual real-time recovery deadline test should prove production
timing; faster test-only injected budgets can cover the rest without exposing
tool-controlled deadline configuration. Save child logs/exit codes and enforce
outer test timeouts so a failed recovery test cannot hang the suite.

Linux can establish shared-protocol behavior and POSIX/Linux lifecycle evidence.
It cannot establish native Windows/macOS/BSD support. Report implemented portable
branches with native qualification outstanding until real runners execute;
avoid either claiming those platforms passed or narrowing support silently.

## Bounded scope and remaining implementation choices

Necessary files are a lightweight MCP launcher/supervisor, private child runner,
MCP child watchdog endpoint/lifespan, a tiny semantic termination adaptation in
the existing watchdog/recovery interface, one closed protocol extension if
needed, and the coordinator drain seam. D2 root authority remains independent.
No change to engine submission/witness algorithms or Web serving topology is
needed merely for portability.

Loopback versus inherited-handle bootstrap was a routine implementation choice
with an explicit environmental tradeoff, not a new OS-support decision; parent
has accepted the loopback baseline. Use one channel and one MCP supervisor
implementation. Native qualification is a
verification limitation, not permission to ship an inert fallback. No new user
approval is needed to prepare/implement this authorized portable scope.
